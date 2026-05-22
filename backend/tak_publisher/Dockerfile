FROM python:3.12-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY backend/tak_publisher/requirements.txt ./requirements.txt
RUN pip install --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PORT=8080

RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY --from=build /install /usr/local
COPY backend/tak_publisher/src ./src

USER app
EXPOSE 8080

CMD ["python", "-m", "tak_publisher.main"]
