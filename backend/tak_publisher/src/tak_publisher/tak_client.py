from __future__ import annotations

import asyncio
import json
import os
import random
import ssl
import tempfile
from dataclasses import dataclass

import structlog
from google.cloud import secretmanager

from .config import settings

log = structlog.get_logger(__name__)


@dataclass
class TakCredentials:
    host: str
    port: int
    use_tls: bool
    client_cert_pem: str | None
    client_key_pem: str | None
    ca_cert_pem: str | None


def load_credentials() -> TakCredentials:
    """Load TAK Server credentials from Secret Manager (or env fallback).

    Expected secret payload JSON shape:
        {
          "host": "tak.example.mil",
          "port": 8089,
          "use_tls": true,
          "client_cert_pem": "-----BEGIN CERTIFICATE-----...",
          "client_key_pem": "-----BEGIN PRIVATE KEY-----...",
          "ca_cert_pem": "-----BEGIN CERTIFICATE-----..."
        }
    """
    if settings.tak_credentials_secret:
        client = secretmanager.SecretManagerServiceClient()
        version_name = settings.tak_credentials_secret
        if "/versions/" not in version_name:
            version_name = f"{version_name}/versions/latest"
        response = client.access_secret_version(request={"name": version_name})
        payload = response.payload.data.decode("utf-8")
        data = json.loads(payload)
        return TakCredentials(
            host=str(data.get("host") or settings.tak_default_host),
            port=int(data.get("port") or settings.tak_default_port),
            use_tls=bool(data.get("use_tls", settings.tak_use_tls)),
            client_cert_pem=data.get("client_cert_pem"),
            client_key_pem=data.get("client_key_pem"),
            ca_cert_pem=data.get("ca_cert_pem"),
        )

    return TakCredentials(
        host=settings.tak_default_host,
        port=settings.tak_default_port,
        use_tls=settings.tak_use_tls,
        client_cert_pem=None,
        client_key_pem=None,
        ca_cert_pem=None,
    )


def _build_ssl_context(creds: TakCredentials) -> ssl.SSLContext | None:
    if not creds.use_tls:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if creds.ca_cert_pem:
        ctx.load_verify_locations(cadata=creds.ca_cert_pem)
    if creds.client_cert_pem and creds.client_key_pem:
        # SSL stdlib only loads cert/key from files; write to a single tempfile.
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".pem",
            delete=False,
        ) as cert_file:
            cert_file.write(creds.client_cert_pem)
            cert_path = cert_file.name
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".pem",
            delete=False,
        ) as key_file:
            key_file.write(creds.client_key_pem)
            key_path = key_file.name
        os.chmod(cert_path, 0o600)
        os.chmod(key_path, 0o600)
        try:
            ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        finally:
            os.unlink(cert_path)
            os.unlink(key_path)
    return ctx


class TakClient:
    """Persistent CoT writer to a TAK Server with reconnect+backoff."""

    def __init__(self, credentials: TakCredentials | None = None) -> None:
        self._credentials = credentials or load_credentials()
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._lock = asyncio.Lock()
        self._attempt = 0

    @property
    def host(self) -> str:
        return self._credentials.host

    @property
    def port(self) -> int:
        return self._credentials.port

    async def connect(self) -> None:
        if not self._credentials.host:
            raise RuntimeError("TAK Server host is not configured")
        ssl_ctx = _build_ssl_context(self._credentials)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(
                host=self._credentials.host,
                port=self._credentials.port,
                ssl=ssl_ctx,
                server_hostname=self._credentials.host if ssl_ctx else None,
            ),
            timeout=settings.tak_connect_timeout_seconds,
        )
        self._attempt = 0
        log.info(
            "tak_connected",
            host=self._credentials.host,
            port=self._credentials.port,
            tls=self._credentials.use_tls,
        )

    async def send(self, payload: bytes) -> None:
        async with self._lock:
            for _ in range(2):
                if self._writer is None:
                    await self._connect_with_backoff()
                try:
                    assert self._writer is not None
                    self._writer.write(payload)
                    await asyncio.wait_for(
                        self._writer.drain(),
                        timeout=settings.tak_write_timeout_seconds,
                    )
                    return
                except (
                    ConnectionError,
                    asyncio.TimeoutError,
                    ssl.SSLError,
                    OSError,
                ) as e:
                    log.warning("tak_write_failed", error=str(e), error_type=type(e).__name__)
                    await self._teardown()
            raise RuntimeError("TAK send failed after reconnect")

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()

    async def _connect_with_backoff(self) -> None:
        while True:
            try:
                await self.connect()
                return
            except Exception as e:  # noqa: BLE001
                self._attempt += 1
                delay = min(
                    settings.tak_max_reconnect_delay_seconds,
                    (1.5 ** min(self._attempt, 8)),
                )
                delay = delay / 2 + random.uniform(0, delay / 2)
                log.warning(
                    "tak_connect_failed",
                    attempt=self._attempt,
                    error=str(e),
                    retry_in_seconds=round(delay, 2),
                )
                await asyncio.sleep(delay)

    async def _teardown(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._reader = None
