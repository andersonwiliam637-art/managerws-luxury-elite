# ManagerW.S Elite Credit Platform

Versión Luxury · Escalable · Lista para producción

## Características principales

- Diseño visual premium estilo Lexus (negro profundo + champagne gold)
- Gestión de clientes y créditos con control de pagos
- Planes de suscripción con precios por país (PPP)
- Panel de administración completo
- Seguridad reforzada (CSRF, rate limiting, headers, validaciones)
- Preparado para PostgreSQL + Gunicorn
- PWA instalable
- Validaciones de montos y prevención de overpayment
- Connection pooling y consultas optimizadas

## Ejecución local

```bash
pip install -r requirements.txt
export SECRET_KEY="una-clave-segura-larga"
export ADMIN_PASSWORD="su-password-seguro"
python app.py
```

Abrir: http://localhost:5000

Usuario administrador por defecto: el definido en ADMIN_USERNAME (por defecto "admin")

## Variables de entorno recomendadas (producción)

- `SECRET_KEY` → clave secreta fuerte
- `DATABASE_URL` → URL de PostgreSQL
- `FLASK_ENV=production`
- `ADMIN_USERNAME` y `ADMIN_PASSWORD`
- `PAYPAL_EMAIL`, `WISE_ACCOUNT`, `WHATSAPP`
- `REDIS_URL` (opcional, para rate limiting compartido)

## Despliegue en Render

1. Subir este repositorio a GitHub
2. Render → New → Web Service → conectar el repo
3. Runtime: Docker
4. Crear base de datos PostgreSQL y copiar DATABASE_URL
5. Agregar las variables de entorno listadas arriba

## Soporte

- PayPal: andersonwiliam@gmail.com
- WhatsApp: +502 5415 4016

## Notas de escalabilidad

- Use PostgreSQL en producción (nunca SQLite con tráfico real)
- Gunicorn con 3 workers + 2 threads está configurado por defecto
- Los listados están limitados (200-300 registros) para evitar sobrecarga
- Para mayor escala se recomienda agregar Redis y paginación completa
