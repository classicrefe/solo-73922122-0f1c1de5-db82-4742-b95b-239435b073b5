FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_DB=/data/settlement.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x ./entrypoint.sh && \
    useradd --create-home --uid 10001 appuser && \
    mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
