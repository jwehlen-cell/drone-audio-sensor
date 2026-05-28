FROM python:3.11-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

COPY backend/inference/requirements.txt ./requirements.txt
RUN pip install --prefix=/install -r requirements.txt


FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PORT=8080 \
    TFHUB_CACHE_DIR=/app/tfhub_cache \
    TF_CPP_MIN_LOG_LEVEL=2

RUN apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system app \
 && useradd --system --gid app --home /app app

WORKDIR /app

COPY --from=build /install /usr/local
COPY backend/inference/src ./src
COPY backend/inference/models ./models

RUN mkdir -p /app/tfhub_cache && chown -R app:app /app

USER app
EXPOSE 8080

CMD ["python", "-m", "inference.main"]
