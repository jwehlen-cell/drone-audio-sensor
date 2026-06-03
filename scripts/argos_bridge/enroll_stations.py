#!/usr/bin/env python3
"""Enroll the 33 simulated SH-* stations in the argosuat Firestore
device registry. Idempotent — re-running just refreshes public keys
and (optionally) bumps the assigned_site_label / location fields.

Reads public keys minted by mint_test_pki.py from ``out_pubkeys/``.

Run-once. Required before the bridge can stream — the gateway looks
up the device's ``public_key_pem`` and ``current_location`` when
processing the connect handshake.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from google.cloud import firestore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stations import STATIONS

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="argosuat")
    parser.add_argument("--collection", default="devices")
    parser.add_argument(
        "--pubkey-dir",
        default=str(Path(__file__).resolve().parent / "out_pubkeys"),
        help="Directory mint_test_pki.py wrote per-station public keys to",
    )
    parser.add_argument(
        "--site-label",
        default="SH",
        help="assigned_site_label written to each device doc (free-form per-station tag)",
    )
    parser.add_argument(
        "--site",
        default="Shaw",
        help="Site grouping key. The admin UI's top selector chooses one of "
             "these; all device + detection queries are scoped to it.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    db = firestore.Client(project=args.project, database="(default)")
    coll = db.collection(args.collection)
    pubdir = Path(args.pubkey_dir)

    # Use the short Argos id (SH011) for the public-key filename so a
    # SIM-prefix rename doesn't need fresh PKI material.
    from stations import short_id  # local import keeps the CLI import-light

    now_ms = int(time.time() * 1000)
    written = 0
    for st in STATIONS:
        pub_path = pubdir / f"{short_id(st.station_id)}.pub.pem"
        if not pub_path.is_file():
            # Backward-compat: older mint runs wrote to {full_id}.pub.pem.
            legacy = pubdir / f"{st.station_id}.pub.pem"
            if legacy.is_file():
                pub_path = legacy
            else:
                log.warning("no public key for %s at %s; skipping", st.station_id, pub_path)
                continue
        pem = pub_path.read_text()
        # site_label gets the descriptive sentence per-station from
        # stations.py; the admin's Site column renders it verbatim,
        # making "SIMULATED – Shaw AFB / Sumter SC cluster ... SH011"
        # the visible name on every row.
        label = st.description or args.site_label
        doc = {
            "device_id": st.station_id,
            "state": "active",
            "site": args.site,
            "assigned_site_label": label,
            "app_version": "argos-sim/1.0",
            "device_model": "argos-bridge",
            "os_version": "argos-sim/1.0",
            "public_key_pem": pem,
            "first_seen_ms": now_ms,
            "last_seen_ms": now_ms,
            "last_handshake_ms": now_ms,
            "current_location": firestore.GeoPoint(st.latitude, st.longitude),
            "location_accuracy_m": 10.0,
            "location_status": "current",
            "location_timestamp_ms": now_ms,
            "admin_notes": st.description,
        }
        coll.document(st.station_id).set(doc, merge=True)
        written += 1
        log.info(
            "enrolled %s (%s) at %.6f,%.6f",
            st.station_id, short_id(st.station_id), st.latitude, st.longitude,
        )

    log.info("done; %d/%d enrolled", written, len(STATIONS))


if __name__ == "__main__":
    main()
