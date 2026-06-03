#!/usr/bin/env python3
"""Mock TAK receiver.

Stand-in for a real TAK Server during R&D. Accepts the raw
Cursor-on-Target (CoT) XML stream that ``backend/tak_publisher``
emits over TCP, parses each ``<event>...</event>`` envelope out of
the wire, and either drops it or persists it for an hour.

Drop-in behaviour (no extra infra):

  - Accepts unencrypted TCP on ``--port`` (default 8089).
  - Logs every parsed event to stdout (and the systemd journal on
    the VM via the unit's StandardOutput).

Optional persistence:

  - ``--firestore`` writes each event to a Firestore collection (default
    ``tak_events``) with ``expires_at = now + 1h``. A one-time TTL policy
    on that field (mirrors the existing ``detections.expires_at`` TTL
    set up by Terraform) reaps old rows automatically.

Why a VM and not Cloud Run: Cloud Run only fronts HTTP/2; TAK is raw
TCP. The persistent socket lifecycle wouldn't survive Cloud Run's
request model anyway.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import re
import signal
import sys
import xml.etree.ElementTree as ET
from typing import Optional

log = logging.getLogger("mock-tak")

_EVENT_OPEN = re.compile(rb"<\?xml[^>]*\?>\s*<event\b", re.S)
_EVENT_CLOSE = b"</event>"


class FirestoreSink:
    """Lazy Firestore client wrapper. Idempotent: a missing client
    library falls through to a no-op so ``--firestore`` can be set
    on a host without the dep available."""

    def __init__(self, collection: str, ttl_seconds: int = 3600) -> None:
        self._collection = collection
        self._ttl_seconds = ttl_seconds
        self._client = None
        try:
            from google.cloud import firestore  # type: ignore
            self._client = firestore.Client()
            log.info("firestore sink ready collection=%s ttl=%ds", collection, ttl_seconds)
        except Exception as e:  # noqa: BLE001
            log.warning("firestore unavailable (%s); persistence disabled", e)

    def write(self, doc_id: str, parsed: dict, raw_xml: bytes) -> None:
        if self._client is None:
            return
        now = dt.datetime.now(tz=dt.timezone.utc)
        payload = {
            **parsed,
            "raw_xml": raw_xml.decode("utf-8", errors="replace"),
            "received_at": now,
            "expires_at": now + dt.timedelta(seconds=self._ttl_seconds),
        }
        try:
            self._client.collection(self._collection).document(doc_id).set(payload)
        except Exception as e:  # noqa: BLE001
            log.warning("firestore write failed doc=%s err=%s", doc_id, e)


def parse_cot(raw: bytes) -> Optional[dict]:
    """Parse a single ``<event>`` envelope into a flat dict matching
    the schema the publisher emits (see backend/tak_publisher/cot.py)."""
    try:
        # The publisher prefixes each event with an XML declaration. ET
        # is happy with either form; strip leading whitespace to keep
        # multi-event streams parseable.
        root = ET.fromstring(raw.strip())
    except ET.ParseError as e:
        log.warning("malformed event: %s | first 80 bytes: %r", e, raw[:80])
        return None

    point = root.find("point")
    detail = root.find("detail")
    contact = detail.find("contact") if detail is not None else None
    remarks = detail.find("remarks") if detail is not None else None

    return {
        "uid": root.get("uid"),
        "type": root.get("type"),
        "time": root.get("time"),
        "start": root.get("start"),
        "stale": root.get("stale"),
        "how": root.get("how"),
        "lat": float(point.get("lat")) if point is not None and point.get("lat") else None,
        "lon": float(point.get("lon")) if point is not None and point.get("lon") else None,
        "callsign": contact.get("callsign") if contact is not None else None,
        "remarks": (remarks.text if remarks is not None else None),
    }


def split_events(buf: bytes) -> tuple[list[bytes], bytes]:
    """Pull complete ``<event>...</event>`` blocks out of a streaming
    buffer. Returns ``(events, leftover)``."""
    events: list[bytes] = []
    while True:
        end = buf.find(_EVENT_CLOSE)
        if end < 0:
            return events, buf
        end_idx = end + len(_EVENT_CLOSE)
        # Walk back to the matching XML prolog or <event> tag, whichever
        # comes first, so we cleanly emit the leading whitespace too.
        prolog = _EVENT_OPEN.search(buf, 0, end_idx)
        if prolog is None:
            # No matching open — drop everything up to this close and
            # keep going. Wire protocol bugs shouldn't wedge the parser.
            buf = buf[end_idx:]
            continue
        # Backtrack from the regex match to include the XML prolog if
        # one immediately precedes it (it always does for our publisher,
        # but we keep this defensive).
        events.append(buf[prolog.start():end_idx])
        buf = buf[end_idx:]


class ConnectionHandler:
    def __init__(self, sink: FirestoreSink, drop_only: bool) -> None:
        self._sink = sink
        self._drop_only = drop_only

    async def __call__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("client connected: %s", peer)
        buf = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buf += chunk
                events, buf = split_events(buf)
                for raw in events:
                    parsed = parse_cot(raw)
                    if parsed is None:
                        continue
                    log.info(
                        "event uid=%s callsign=%s lat=%s lon=%s",
                        parsed.get("uid"),
                        parsed.get("callsign"),
                        parsed.get("lat"),
                        parsed.get("lon"),
                    )
                    if not self._drop_only:
                        doc_id = parsed.get("uid") or f"anon-{dt.datetime.now().isoformat()}"
                        self._sink.write(doc_id, parsed, raw)
        except (asyncio.CancelledError, ConnectionError) as e:
            log.info("connection ended (%s): %s", type(e).__name__, peer)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            log.info("client disconnected: %s", peer)


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    sink = FirestoreSink(args.collection, ttl_seconds=args.ttl_seconds)
    handler = ConnectionHandler(sink, drop_only=args.drop_only)

    server = await asyncio.start_server(handler, args.host, args.port)
    sockets = server.sockets or []
    for s in sockets:
        log.info("listening on %s", s.getsockname())

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    async with server:
        await asyncio.wait(
            [asyncio.create_task(server.serve_forever()), asyncio.create_task(shutdown.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
    log.info("mock-tak shutting down")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("MOCK_TAK_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MOCK_TAK_PORT", "8089"))
    )
    parser.add_argument(
        "--collection",
        default=os.environ.get("MOCK_TAK_COLLECTION", "tak_events"),
        help="Firestore collection for persisted events",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=int(os.environ.get("MOCK_TAK_TTL_SECONDS", "3600")),
        help="expires_at = now + ttl. Pair with a Firestore TTL policy on this field.",
    )
    parser.add_argument(
        "--drop-only",
        action="store_true",
        default=os.environ.get("MOCK_TAK_DROP_ONLY", "").lower() in {"true", "1", "yes"},
        help="Log and discard; do not write to Firestore",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
