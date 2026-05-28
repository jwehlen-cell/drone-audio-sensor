FROM python:3.12-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY backend/gateway/requirements.txt ./requirements.txt
RUN pip install --prefix=/install -r requirements.txt

COPY proto/ ./proto/
# pip --prefix=/install puts packages under /install/lib/.../site-packages,
# which is not on the build stage's default PYTHONPATH, so the protoc
# step needs to be pointed at it explicitly.
RUN mkdir -p /install/proto_gen \
 && PYTHONPATH=/install/lib/python3.12/site-packages \
    python -m grpc_tools.protoc \
      -I./proto \
      --python_out=/install/proto_gen \
      --pyi_out=/install/proto_gen \
      --grpc_python_out=/install/proto_gen \
      ./proto/drone_audio.proto


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app/proto_gen \
    PORT=50051

RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY --from=build /install /usr/local
COPY --from=build /install/proto_gen ./proto_gen
COPY backend/gateway/src ./src

USER app
EXPOSE 50051

CMD ["python", "-m", "gateway.main"]
