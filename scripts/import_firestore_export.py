"""Restore the Firestore collections captured by the 2026-05-29 teardown.

Reads each ``<EXPORT_DIR>/firestore/<collection>.jsonl`` and writes the
documents into the configured Firestore project's ``(default)`` database,
preserving original doc IDs and decoding the type sentinels written by
the export pass (``{"__type__": "timestamp" | "geopoint" | "ref" | "bytes"}``).

Run:
    NEW_PROJECT=<id> EXPORT_DIR=teardown_export_<ts> \
        scripts/.venv/bin/python scripts/import_firestore_export.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from google.cloud import firestore
from google.cloud.firestore_v1 import GeoPoint

PROJECT = os.environ.get("NEW_PROJECT")
EXPORT_DIR = os.environ.get("EXPORT_DIR")

if not PROJECT or not EXPORT_DIR:
    sys.exit("Set NEW_PROJECT and EXPORT_DIR env vars; see docs/RECREATE.md step 4.")

EXPORT = Path(EXPORT_DIR) / "firestore"
if not EXPORT.is_dir():
    sys.exit(f"Missing {EXPORT}; the export dir must contain a 'firestore/' subdir.")


def _dec(v):
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if isinstance(v, dict):
        if "__type__" in v:
            t = v["__type__"]
            if t == "timestamp":
                return datetime.fromisoformat(v["iso"])
            if t == "geopoint":
                return GeoPoint(v["lat"], v["lng"])
            if t == "bytes":
                return base64.b64decode(v["b64"])
            if t == "ref":
                # Re-resolve relative to the target project — the original
                # ref's path is preserved.
                return client.document(v["path"])
            return {k: _dec(x) for k, x in v.items()}
        return {k: _dec(x) for k, x in v.items()}
    return v


client = firestore.Client(project=PROJECT)
print(f"Target: project={PROJECT}, db=(default)")

for jsonl in sorted(EXPORT.glob("*.jsonl")):
    col_id = jsonl.stem
    col = client.collection(col_id)
    n = 0
    with jsonl.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            doc_id = doc.pop("__id__")
            col.document(doc_id).set(_dec(doc))
            n += 1
    print(f"  {col_id:<20} -> {n} docs restored")

print("Done.")
