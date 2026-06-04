#!/usr/bin/env python3
"""One-shot: backfill the ``site`` field on every device doc in the
argosuat Firestore device registry.

Mapping rules:
  DRONE-SENSOR-*   -> Patrick   (existing simulator fleet)
  SH*              -> Shaw      (argos-bridge stations)
  anything else    -> printed for manual classification, skipped

Idempotent: re-running just refreshes the site value, doesn't disturb
other fields.
"""
from __future__ import annotations

import argparse
import logging
import sys

from google.cloud import firestore

log = logging.getLogger(__name__)


def site_for(device_id: str) -> str | None:
    # Current naming (SIM-prefixed simulators + real PHONE-* phones).
    # Five-base layout for the 500-phone load test.
    if device_id.startswith("SIM-PATRICK-"):
        return "Patrick"
    if device_id.startswith("ARGOS-SHAW-") or device_id.startswith("SIM-SHAW-"):
        # Live Argos pull (ARGOS-SHAW-*) and any legacy replay-bridge
        # docs (SIM-SHAW-*) both bucket under the Shaw site chip.
        return "Shaw"
    if device_id.startswith("SIM-LANGLEY-"):
        return "Langley"
    if device_id.startswith("SIM-VANDENBERG-"):
        return "Vandenberg"
    if device_id.startswith("SIM-NELLIS-"):
        return "Nellis"
    if device_id.startswith("SIM-HICKAM-"):
        return "Hickam"
    if device_id.startswith("SIM-WPAFB-"):
        return "WrightPatterson"
    if device_id.startswith("SIM-EIELSON-"):
        return "Eielson"
    if device_id.startswith("SIM-ANDERSEN-"):
        return "Andersen"
    if device_id.startswith("SIM-KADENA-"):
        return "Kadena"
    if device_id.startswith("SIM-RAMSTEIN-"):
        return "Ramstein"
    if device_id.startswith("SIM-BUCKLEY-"):
        return "Buckley"
    if device_id.startswith("PHONE-PATRICK-"):
        return "Patrick"
    # Legacy IDs that predate the SIM-prefix rename — kept so
    # re-running the backfill on an older snapshot still classifies
    # everything correctly.
    if device_id.startswith("DRONE-SENSOR-"):
        return "Patrick"
    if device_id.startswith("SH"):
        return "Shaw"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="argosuat")
    parser.add_argument("--collection", default="devices")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print intended changes without writing"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    db = firestore.Client(project=args.project, database="(default)")
    coll = db.collection(args.collection)

    changed = 0
    unknown: list[str] = []
    for snap in coll.stream():
        device_id = snap.id
        data = snap.to_dict() or {}
        current = data.get("site")
        target = site_for(device_id)
        if target is None:
            unknown.append(device_id)
            continue
        if current == target:
            continue
        log.info(
            "%s: site=%r -> %r%s",
            device_id,
            current,
            target,
            " (dry-run)" if args.dry_run else "",
        )
        if not args.dry_run:
            coll.document(device_id).set({"site": target}, merge=True)
        changed += 1

    if unknown:
        log.warning(
            "%d device(s) didn't match any site rule; left untouched: %s",
            len(unknown),
            unknown,
        )
    log.info("done; %d device(s) updated", changed)


if __name__ == "__main__":
    sys.exit(main() or 0)
