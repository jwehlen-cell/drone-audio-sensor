#!/usr/bin/env python3
"""Seed simulated phone device docs for the multi-base test.

Adds 100 placeholder ``SIM-{BASE}-###`` device docs per requested base
to the argosuat Firestore device registry. The docs carry the site
grouping key, a descriptive ``assigned_site_label`` for the admin's
Site column, and a per-phone lat/lon scattered around the base centre
so the dashboard map shows them at the right place. They're seeded as
``state=active`` with no live Redis state — the admin's Status page
hides them until a real handshake arrives. The /registered page shows
them immediately, and the site chip selector picks them up via
list_sites().

Run as a one-shot:

    .venv/bin/python scripts/seed_test_bases.py --project=argosuat

Re-running is idempotent: any existing doc with the same id is merged,
not overwritten.
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import random
import sys
import time
from dataclasses import dataclass

from google.cloud import firestore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Base:
    site: str            # grouping key (admin site chip)
    callsign: str        # short prefix for SIM-CALLSIGN-### device ids
    label: str           # human-readable in Site column
    center_lat: float
    center_lon: float
    radius_km: float = 1.5


# Five bases for the 500-phone test. Patrick + Shaw match the user's
# existing site groupings; the three additions span both coasts +
# interior so the map renders something interesting at every zoom.
BASES: tuple[Base, ...] = (
    Base("Patrick",        "PATRICK",   "Patrick SFB, FL (Cape Canaveral coast)",          28.235,    -80.6005),
    Base("Shaw",           "SHAW",      "Shaw AFB, SC (Sumter)",                            33.971,    -80.461),
    Base("Langley",        "LANGLEY",   "Langley AFB, VA (Hampton, mid-Atlantic)",          37.0833,   -76.3603),
    Base("Vandenberg",     "VANDENBERG","Vandenberg SFB, CA (Lompoc, west-coast space)",    34.7420,   -120.5724),
    Base("Nellis",         "NELLIS",    "Nellis AFB, NV (north of Las Vegas, desert)",      36.2356,   -115.0344),
    Base("Hickam",         "HICKAM",    "JB Pearl Harbor–Hickam, HI (Oahu, tropical Pacific)", 21.3286, -157.9472),
    Base("WrightPatterson","WPAFB",     "Wright-Patterson AFB, OH (Dayton, Midwest)",       39.8138,   -84.0494),
)


def scatter(center_lat: float, center_lon: float, radius_km: float, rng: random.Random
            ) -> tuple[float, float]:
    """Pick a point uniformly inside a circle of ``radius_km`` around
    the centre. Latitude degree ≈ 111 km; longitude degree shrinks with
    cos(lat). Good enough for a base-sized footprint."""
    r = radius_km * math.sqrt(rng.random())
    theta = rng.uniform(0, 2 * math.pi)
    dlat = (r / 111.0) * math.cos(theta)
    dlon = (r / (111.0 * math.cos(math.radians(center_lat)))) * math.sin(theta)
    return center_lat + dlat, center_lon + dlon


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="argosuat")
    parser.add_argument("--collection", default="devices")
    parser.add_argument(
        "--phones-per-base", type=int, default=100,
        help="How many SIM-{CALLSIGN}-### docs to seed at each base.",
    )
    parser.add_argument(
        "--bases", default="Langley,Vandenberg,Nellis",
        help="Comma-separated subset of base site keys to seed. Default "
             "adds the three new bases; pass 'all' to also (re)seed Patrick + Shaw.",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="RNG seed for the per-phone lat/lon scatter (so reruns are stable).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if args.bases.strip().lower() == "all":
        wanted_sites = {b.site for b in BASES}
    else:
        wanted_sites = {s.strip() for s in args.bases.split(",") if s.strip()}
    targets = [b for b in BASES if b.site in wanted_sites]
    if not targets:
        log.error("no matching base in --bases=%r", args.bases)
        return 1

    rng = random.Random(args.seed)
    db = firestore.Client(project=args.project, database="(default)")
    coll = db.collection(args.collection)

    now_ms = int(time.time() * 1000)
    written = 0
    for base in targets:
        log.info(
            "seeding %d phones at %s (%s)",
            args.phones_per_base, base.site, base.label,
        )
        for i in range(1, args.phones_per_base + 1):
            lat, lon = scatter(base.center_lat, base.center_lon, base.radius_km, rng)
            did = f"SIM-{base.callsign}-{i:03d}"
            doc = {
                "device_id": did,
                "state": "active",
                "site": base.site,
                # The admin's Site column reads assigned_site_label
                # verbatim, then the Site/Type splitter strips a trailing
                # "(...)". Keeping the suffix here means the Type column
                # gets "sim/phone" populated alongside the base label.
                "assigned_site_label": f"SIMULATED – {base.label} (sim/phone)",
                "app_version": "sim-fleet-1.0",
                "device_model": "simulated-phone",
                "os_version": "sim",
                "first_seen_ms": now_ms,
                "current_location": firestore.GeoPoint(lat, lon),
                "location_accuracy_m": 10.0,
                "location_status": "current",
                "location_timestamp_ms": now_ms,
                "admin_notes": (
                    f"500-phone load-test seed: {base.label}. "
                    f"Center ({base.center_lat:.4f}, {base.center_lon:.4f}); "
                    f"scattered uniformly within {base.radius_km} km."
                ),
            }
            coll.document(did).set(doc, merge=True)
            written += 1
    log.info("done; %d device docs seeded across %d base(s)", written, len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
