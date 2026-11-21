FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FRIGO_DATA_DIR=/app/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 1000 --create-home appuser \
    && mkdir -p /app/data \
    && chown appuser /app/data

COPY app ./app
USER appuser
EXPOSE 8000
VOLUME /app/data

# --no-proxy-headers : l'IP vue doit rester celle de nginx, que FRIGO_TRUSTED_PROXIES vérifie.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
