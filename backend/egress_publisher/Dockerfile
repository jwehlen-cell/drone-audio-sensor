FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY backend/egress_publisher/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Source + generated proto stubs are baked at build time. The
# protoc step happens above in cloudbuild before docker build sees
# the context, so the stub is on disk by the time we COPY.
COPY backend/egress_publisher/src /app/src

ENV PYTHONPATH=/app/src

# Cloud Run health check expects HTTP on $PORT.
ENV PORT=8080
EXPOSE 8080

CMD ["python", "-m", "egress_publisher.main"]
