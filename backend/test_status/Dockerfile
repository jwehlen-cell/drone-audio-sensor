FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY backend/test_status/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend/test_status/src /app/src
ENV PYTHONPATH=/app/src
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-m", "test_status.main"]
