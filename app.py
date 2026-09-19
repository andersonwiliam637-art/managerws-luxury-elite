import os
import secrets
import logging
import string
from datetime import datetime
from functools import wraps

import requests as req_lib
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.orm import joinedload

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
app = Flask(__name__)

app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False,
    PERMANENT_SESSION_LIFETIME=3600 * 6,
    WTF_CSRF_TIME_LIMIT=None,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={
        'pool_pre_ping': True,
        'pool_recycle': 280,
        'pool_size': 5,
        'max_overflow': 8,
    }
)

if os.environ.get('FLASK_ENV') == 'production':
    app.config['SESSION_COOKIE_SECURE'] = True

csrf = CSRFProtect(app)
storage_uri = os.environ.get('REDIS_URL', 'memory://')
limiter = Limiter(get_remote_address, app=app, storage_uri=storage_uri, default_limits=["150 per hour"])

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('managerws')

# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
db_url = os.environ.get('DATABASE_URL')
if db_url and db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url if db_url else 'sqlite:///managerws_gt.db'
db = SQLAlchemy(app)

# ---------------------------------------------------------------------------
# Constantes de negocio
# ---------------------------------------------------------------------------
CURRENCIES = {
    'GTQ': 'Q', 'USD': '$', 'HNL': 'L', 'CRC': '₡',
    'NIO': 'C$', 'MXN': '$'
}

# Precios mensuales por país (PPP)
PLANS_MONTHLY = {
    'GT': {'name': 'Guatemala', 'currency': 'GTQ', 'symbol': 'Q', 'pro': 99, 'business': 249, 'enterprise': 499},
    'SV': {'name': 'El Salvador', 'currency': 'USD', 'symbol': '$', 'pro': 12, 'business': 29, 'enterprise': 59},
    'HN': {'name': 'Honduras', 'currency': 'HNL', 'symbol': 'L', 'pro': 299, 'business': 699, 'enterprise': 1399},
    'NI': {'name': 'Nicaragua', 'currency': 'NIO', 'symbol': 'C$', 'pro': 450, 'business': 1099, 'enterprise': 2199},
    'CR': {'name': 'Costa Rica', 'currency': 'CRC', 'symbol': '₡', 'pro': 6990, 'business': 16990, 'enterprise': 33990},
    'PA': {'name': 'Panamá', 'currency': 'USD', 'symbol': '$', 'pro': 14, 'business': 34, 'enterprise': 69},
    'MX': {'name': 'México', 'currency': 'MXN', 'symbol': '$', 'pro': 249, 'business': 599, 'enterprise': 1199},
    'US': {'name': 'Estados Unidos', 'currency': 'USD', 'symbol': '$', 'pro': 19, 'business': 49, 'enterprise': 99},
}

# Pago único (Licencia de por vida) - precio favorable para negociantes
# Se expresa en USD para simplicidad de cobro internacional
LIFETIME_USD = {
    'pro': 149,          # ~12 meses de Pro
    'business': 299,     # ~9-10 meses de Business
    'enterprise': 499,   # ~8 meses de Enterprise + valor
}

USD_MONTHLY = {'pro': 12.99, 'business': 32.99, 'enterprise': 64.99}

PAYMENT_CONFIG = {
    'paypal_email': os.environ.get('PAYPAL_EMAIL', 'andersonwiliam@gmail.com'),
    'wise': os.environ.get('WISE_ACCOUNT', 'tu_correo@wise.com'),
    'bank_gt': os.environ.get('BANK_GT', 'Banco Industrial Bi-1402158 a willy enriquez'),
    'whatsapp': os.environ.get('WHATSAPP', '+50254154016'),
}

COMMISSION_RATE = 0.25  # 25% de comisión por referido en el primer pago

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def money(amount, cur='GTQ'):
    try:
        return f"{CURRENCIES.get(cur, 'Q')} {float(amount):,.2f}"
    except (TypeError, ValueError):
        return f"{CURRENCIES.get(cur, 'Q')} 0.00"


def safe_float(value, default=0.0):
    try:
        v = float(value)
        return max(0.0, v)
    except (TypeError, ValueError):
        return default


def detect_country():
    try:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ip and ',' in ip:
            ip = ip.split(',')[0].strip()
        r = req_lib.get(f'http://ip-api.com/json/{ip}?fields=countryCode', timeout=2)
        code = r.json().get('countryCode', 'GT')
        return code if code in PLANS_MONTHLY else 'GT'
    except Exception:
        return 'GT'


def generate_referral_code(length=8):
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------
class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='client', index=True)
    is_active_subscription = db.Column(db.Boolean, default=False, index=True)
    currency = db.Column(db.String(10), default='GTQ')
    plan = db.Column(db.String(50), default='Gratis')
    plan_type = db.Column(db.String(20), default='monthly')  # monthly | lifetime
    referral_code = db.Column(db.String(12), unique=True, index=True)
    referred_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    clients = db.relationship('Client', backref='owner', lazy='dynamic')
    credits = db.relationship('Credit', backref='owner', lazy='dynamic')
    commissions = db.relationship('Commission', backref='affiliate', lazy='dynamic',
                                  foreign_keys='Commission.affiliate_id')


class Client(db.Model):
    __tablename__ = 'client'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)

    credits = db.relationship('Credit', backref='client', lazy='dynamic')


class Credit(db.Model):
    __tablename__ = 'credit'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False, index=True)
    total = db.Column(db.Float, nullable=False)
    paid = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default='active', index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    payments = db.relationship('Payment', backref='credit', lazy='dynamic', cascade='all, delete-orphan')

    @property
    def pending(self):
        return max(0.0, round(self.total - self.paid, 2))


class Payment(db.Model):
    __tablename__ = 'payment'
    id = db.Column(db.Integer, primary_key=True)
    credit_id = db.Column(db.Integer, db.ForeignKey('credit.id'), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    note = db.Column(db.String(120))


class PaymentRequest(db.Model):
    __tablename__ = 'payment_request'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    user = db.relationship('User')
    plan = db.Column(db.String(50), nullable=False)
    plan_type = db.Column(db.String(20), default='monthly')  # monthly | lifetime
    method = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default='USD')
    reference = db.Column(db.String(120))
    status = db.Column(db.String(20), default='pending', index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Commission(db.Model):
    __tablename__ = 'commission'
    id = db.Column(db.Integer, primary_key=True)
    affiliate_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    referred_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    payment_request_id = db.Column(db.Integer, db.ForeignKey('payment_request.id'))
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default='USD')
    status = db.Column(db.String(20), default='pending')  # pending | paid | cancelled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Decoradores
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if 'user_id' not in session:
            flash('Debe iniciar sesión.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrap


def admin_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('Acceso restringido.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrap


def subscription_active(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        user = User.query.get(session['user_id'])
        if not user or not user.is_active_subscription:
            flash('Su suscripción no está activa. Active un plan para continuar.', 'warning')
            return redirect(url_for('pricing'))
        return f(*args, **kwargs)
    return wrap


# ---------------------------------------------------------------------------
# Seguridad
# ---------------------------------------------------------------------------
@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if os.environ.get('FLASK_ENV') == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


# ---------------------------------------------------------------------------
# Rutas principales
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    if session.get('role') == 'admin':
        return redirect(url_for('admin_dashboard'))
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit('12 per minute')
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        if not username or not password:
            flash('Usuario y contraseña son obligatorios.', 'danger')
            return render_template('login.html')

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session.clear()
            session['user_id'] = user.id
            session['role'] = user.role
            session.permanent = True
            user.last_login = datetime.utcnow()
            db.session.commit()
            logger.info(f'Login OK: {username}')
            return redirect(url_for('index'))

        flash('Credenciales incorrectas.', 'danger')
        logger.warning(f'Login fallido: {username}')

    return render_template('login.html')


@app.route('/register', methods=['POST'])
@limiter.limit('6 per minute')
def register():
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    currency = request.form.get('currency', 'GTQ')
    referral = (request.form.get('referral_code') or '').strip().upper()

    if len(username) < 3:
        flash('El usuario debe tener al menos 3 caracteres.', 'danger')
        return redirect(url_for('login'))

    if len(password) < 6:
        flash('La contraseña debe tener al menos 6 caracteres.', 'danger')
        return redirect(url_for('login'))

    if User.query.filter_by(username=username).first():
        flash('Ese nombre de usuario ya está registrado.', 'danger')
        return redirect(url_for('login'))

    if currency not in CURRENCIES:
        currency = 'GTQ'

    referred_by_id = None
    if referral:
        ref_user = User.query.filter_by(referral_code=referral).first()
        if ref_user:
            referred_by_id = ref_user.id

    # Generar código de referido único
    code = generate_referral_code()
    while User.query.filter_by(referral_code=code).first():
        code = generate_referral_code()

    user = User(
        username=username,
        password=generate_password_hash(password, method='pbkdf2:sha256'),
        currency=currency,
        referral_code=code,
        referred_by=referred_by_id
    )
    db.session.add(user)
    db.session.commit()
    flash('Cuenta creada. Elija un plan para activarla.', 'success')
    return redirect(url_for('login'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))

    credits = Credit.query.filter_by(user_id=user.id).options(joinedload(Credit.payments)).all()
    clients_count = Client.query.filter_by(user_id=user.id, is_active=True).count()

    total_lent = sum(c.total for c in credits)
    total_paid = sum(c.paid for c in credits)
    pending = total_lent - total_paid

    labels, data = [], []
    now = datetime.utcnow()
    for i in range(5, -1, -1):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        labels.append(['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'][m-1])
        month_total = sum(p.amount for c in credits for p in c.payments
                          if p.date and p.date.month == m and p.date.year == y)
        data.append(round(month_total, 2))

    statuses = {
        'Al día': sum(1 for c in credits if c.status == 'active'),
        'Pagado': sum(1 for c in credits if c.status == 'paid'),
    }

    # Comisiones del usuario
    my_commissions = Commission.query.filter_by(affiliate_id=user.id).all()
    pending_commission = sum(c.amount for c in my_commissions if c.status == 'pending')

    return render_template(
        'dashboard.html',
        user=user,
        money=money,
        total_lent=total_lent,
        total_paid=total_paid,
        pending=pending,
        labels=labels,
        data=data,
        statuses=statuses,
        clients_count=clients_count,
        pay=PAYMENT_CONFIG,
        pending_commission=pending_commission,
        my_commissions=my_commissions
    )


@app.route('/clients', methods=['GET', 'POST'])
@login_required
@subscription_active
def clients():
    user = User.query.get(session['user_id'])

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        if len(name) < 2:
            flash('El nombre del cliente es obligatorio.', 'danger')
            return redirect(url_for('clients'))
        client = Client(user_id=user.id, name=name, phone=phone or None)
        db.session.add(client)
        db.session.commit()
        flash('Cliente registrado.', 'success')
        return redirect(url_for('clients'))

    clients_list = Client.query.filter_by(user_id=user.id, is_active=True).order_by(Client.name).all()
    credits_list = (
        Credit.query.filter_by(user_id=user.id)
        .options(joinedload(Credit.client))
        .order_by(Credit.created_at.desc())
        .limit(200).all()
    )

    return render_template(
        'clients.html',
        clients=clients_list,
        credits=credits_list,
        money=money,
        user=user,
        pay=PAYMENT_CONFIG
    )


@app.route('/credit/new', methods=['POST'])
@login_required
@subscription_active
def new_credit():
    client_id = request.form.get('client_id')
    total = safe_float(request.form.get('total'))

    if total <= 0:
        flash('El monto del crédito debe ser mayor a cero.', 'danger')
        return redirect(url_for('clients'))

    client = Client.query.filter_by(id=client_id, user_id=session['user_id']).first()
    if not client:
        flash('Cliente no válido.', 'danger')
        return redirect(url_for('clients'))

    credit = Credit(user_id=session['user_id'], client_id=client.id, total=total)
    db.session.add(credit)
    db.session.commit()
    flash('Crédito creado correctamente.', 'success')
    return redirect(url_for('clients'))


@app.route('/payment/new', methods=['POST'])
@login_required
@subscription_active
def new_payment():
    credit_id = request.form.get('credit_id')
    amount = safe_float(request.form.get('amount'))

    if amount <= 0:
        flash('El monto del pago debe ser mayor a cero.', 'danger')
        return redirect(url_for('clients'))

    credit = Credit.query.filter_by(id=credit_id, user_id=session['user_id']).first()
    if not credit:
        flash('Crédito no válido.', 'danger')
        return redirect(url_for('clients'))

    if amount > credit.pending + 0.01:
        flash(f'El pago no puede superar el saldo pendiente ({money(credit.pending, credit.owner.currency)}).', 'danger')
        return redirect(url_for('clients'))

    payment = Payment(credit_id=credit.id, amount=amount)
    credit.paid = round(credit.paid + amount, 2)
    if credit.paid >= credit.total - 0.01:
        credit.status = 'paid'
        credit.paid = credit.total

    db.session.add(payment)
    db.session.commit()
    flash('Pago registrado correctamente.', 'success')
    return redirect(url_for('clients'))


@app.route('/planes')
def pricing():
    country = detect_country()
    plan = PLANS_MONTHLY.get(country, PLANS_MONTHLY['GT'])
    return render_template(
        'pricing.html',
        plan=plan,
        country=country,
        pay=PAYMENT_CONFIG,
        usd_monthly=USD_MONTHLY,
        lifetime=LIFETIME_USD,
        logged='user_id' in session
    )


@app.route('/checkout', methods=['POST'])
@login_required
@limiter.limit('8 per hour')
def checkout():
    user = User.query.get(session['user_id'])
    country = detect_country()
    plan_data = PLANS_MONTHLY.get(country, PLANS_MONTHLY['GT'])

    plan = request.form.get('plan')
    plan_type = request.form.get('plan_type', 'monthly')
    method = request.form.get('method')
    reference = (request.form.get('reference') or '').strip()[:100]

    if plan not in ('pro', 'business', 'enterprise'):
        flash('Plan no válido.', 'danger')
        return redirect(url_for('pricing'))

    if method not in ('paypal', 'wise', 'bank'):
        flash('Método de pago no válido.', 'danger')
        return redirect(url_for('pricing'))

    if plan_type == 'lifetime':
        amount = LIFETIME_USD[plan]
        currency = 'USD'
    else:
        amount = plan_data[plan]
        currency = plan_data['currency']

    pr = PaymentRequest(
        user_id=user.id,
        plan=plan,
        plan_type=plan_type,
        method=method,
        amount=amount,
        currency=currency,
        reference=reference or None
    )
    db.session.add(pr)
    db.session.commit()
    flash('Solicitud enviada. El administrador verificará el pago y activará su plan.', 'success')
    return redirect(url_for('pricing'))


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.route('/admin')
@admin_required
def admin_dashboard():
    users = User.query.filter_by(role='client').order_by(User.created_at.desc()).limit(300).all()
    payments = (
        PaymentRequest.query.filter_by(status='pending')
        .options(joinedload(PaymentRequest.user))
        .order_by(PaymentRequest.created_at.desc()).all()
    )
    commissions = Commission.query.filter_by(status='pending').order_by(Commission.created_at.desc()).limit(50).all()
    active = sum(1 for u in users if u.is_active_subscription)

    return render_template(
        'admin.html',
        users=users,
        payments=payments,
        commissions=commissions,
        active=active,
        total=len(users),
        inactive=len(users) - active,
        pay=PAYMENT_CONFIG
    )


@app.route('/admin/toggle/<int:id>')
@admin_required
def toggle_user(id):
    user = User.query.get_or_404(id)
    if user.role == 'admin':
        flash('No se puede modificar el administrador principal.', 'danger')
        return redirect(url_for('admin_dashboard'))
    user.is_active_subscription = not user.is_active_subscription
    db.session.commit()
    flash('Estado actualizado.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/plan/<int:id>', methods=['POST'])
@admin_required
def change_plan(id):
    user = User.query.get_or_404(id)
    plan = request.form.get('plan', 'Gratis')
    if plan not in ('Gratis', 'pro', 'business', 'enterprise'):
        flash('Plan no válido.', 'danger')
        return redirect(url_for('admin_dashboard'))
    user.plan = plan
    db.session.commit()
    flash('Plan actualizado.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/payments/<int:id>/approve')
@admin_required
def approve_payment(id):
    pr = PaymentRequest.query.get_or_404(id)
    if pr.status != 'pending':
        flash('Esta solicitud ya fue procesada.', 'warning')
        return redirect(url_for('admin_dashboard'))

    pr.status = 'approved'
    pr.user.is_active_subscription = True
    pr.user.plan = pr.plan
    pr.user.plan_type = pr.plan_type

    # Generar comisión si el usuario fue referido
    if pr.user.referred_by:
        commission_amount = round(pr.amount * COMMISSION_RATE, 2)
        commission = Commission(
            affiliate_id=pr.user.referred_by,
            referred_user_id=pr.user.id,
            payment_request_id=pr.id,
            amount=commission_amount,
            currency=pr.currency,
            status='pending'
        )
        db.session.add(commission)

    db.session.commit()
    flash('Pago aprobado y usuario activado. Comisión generada si aplica.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/payments/<int:id>/reject')
@admin_required
def reject_payment(id):
    pr = PaymentRequest.query.get_or_404(id)
    if pr.status != 'pending':
        flash('Esta solicitud ya fue procesada.', 'warning')
        return redirect(url_for('admin_dashboard'))
    pr.status = 'rejected'
    db.session.commit()
    flash('Pago rechazado.', 'warning')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/commissions/<int:id>/pay')
@admin_required
def mark_commission_paid(id):
    c = Commission.query.get_or_404(id)
    c.status = 'paid'
    db.session.commit()
    flash('Comisión marcada como pagada.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/terminos')
def terms():
    return render_template('terms.html', now=datetime.now())


@app.route('/privacidad')
def privacy():
    return render_template('privacy.html')


@app.route('/sw.js')
def sw():
    return app.send_static_file('sw.js')


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()

    admin_user = os.environ.get('ADMIN_USERNAME', 'admin')
    admin_pass = os.environ.get('ADMIN_PASSWORD')

    if not User.query.filter_by(username=admin_user).first():
        if not admin_pass:
            admin_pass = 'admin2026'
            logger.warning('ADMIN_PASSWORD no definido. Usando valor temporal de desarrollo.')
        hashed = generate_password_hash(admin_pass, method='pbkdf2:sha256')
        code = generate_referral_code()
        db.session.add(User(
            username=admin_user,
            password=hashed,
            role='admin',
            is_active_subscription=True,
            plan='OWNER',
            plan_type='lifetime',
            referral_code=code
        ))
        db.session.commit()
        logger.info(f'Administrador "{admin_user}" creado.')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_ENV') != 'production')
