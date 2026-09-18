FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN adduser --disabled-password --gecos '' appuser && chown -R appuser /app
USER appuser

# Render inyecta $PORT. Forzamos binding correcto.
CMD gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 2 --threads 2 --timeout 90 --access-logfile - --error-logfile - app:app
