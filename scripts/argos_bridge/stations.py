"""Argos-UAT replay simulator roster.

33 simulated stations whose device_ids and audio are SIM-* surrogates
for the original Argos SH-* stations. The argos-bridge pulls each
station's audio from the prod argos GCS bucket (read-only) and
replays it through the UAT gateway; nothing here represents live
sensor data.

Device IDs prefix the original Argos identifier with ``SIM-SHAW-`` so
operators reading the admin dashboard immediately know these rows
aren't live phones. The Shaw label reflects the user-configured site
grouping (most of the SH-* stations sit in the Shaw AFB / Sumter SC
cluster); the four outliers near Cape Canaveral keep the SHAW prefix
for consistency, with their actual location surfaced via the
description field.

Coordinates are the public Argos station positions. The description
text powers the admin's Site column verbatim, so each row reads as a
sentence rather than an opaque ID.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    station_id: str
    latitude: float
    longitude: float
    description: str


def _shaw_desc(sh_id: str, lat: float) -> str:
    return (
        f"SIMULATED – Shaw AFB / Sumter SC cluster (argos-bridge replay), "
        f"station {sh_id} ({lat:.3f}°N)"
    )


def _patrick_outlier_desc(sh_id: str, lat: float) -> str:
    return (
        f"SIMULATED – Patrick SFB FL outlier (argos-bridge replay), "
        f"station {sh_id} ({lat:.3f}°N) – physically not at Shaw"
    )


def _sumter_south_desc(sh_id: str, lat: float) -> str:
    return (
        f"SIMULATED – south SC outlier (argos-bridge replay), "
        f"station {sh_id} ({lat:.3f}°N) – physically not at Shaw"
    )


STATIONS: tuple[Station, ...] = (
    Station("SIM-SHAW-SH000", 28.160115, -80.666448, _patrick_outlier_desc("SH000", 28.160115)),
    Station("SIM-SHAW-SH002", 33.989119, -80.454027, _shaw_desc("SH002", 33.989119)),
    Station("SIM-SHAW-SH003", 33.974757, -80.445196, _shaw_desc("SH003", 33.974757)),
    Station("SIM-SHAW-SH004", 33.968807, -80.447180, _shaw_desc("SH004", 33.968807)),
    Station("SIM-SHAW-SH005", 33.985686, -80.455629, _shaw_desc("SH005", 33.985686)),
    Station("SIM-SHAW-SH006", 33.963007, -80.466037, _shaw_desc("SH006", 33.963007)),
    Station("SIM-SHAW-SH007", 33.965554, -80.445582, _shaw_desc("SH007", 33.965554)),
    Station("SIM-SHAW-SH008", 33.977136, -80.447279, _shaw_desc("SH008", 33.977136)),
    Station("SIM-SHAW-SH009", 33.990082, -80.461216, _shaw_desc("SH009", 33.990082)),
    Station("SIM-SHAW-SH010", 33.987692, -80.459884, _shaw_desc("SH010", 33.987692)),
    Station("SIM-SHAW-SH011", 33.969347, -80.441949, _shaw_desc("SH011", 33.969347)),
    Station("SIM-SHAW-SH012", 33.969537, -80.464734, _shaw_desc("SH012", 33.969537)),
    Station("SIM-SHAW-SH013", 33.965186, -80.449219, _shaw_desc("SH013", 33.965186)),
    Station("SIM-SHAW-SH014", 33.961795, -80.469965, _shaw_desc("SH014", 33.961795)),
    Station("SIM-SHAW-SH015", 33.969073, -80.443608, _shaw_desc("SH015", 33.969073)),
    Station("SIM-SHAW-SH016", 33.971952, -80.442491, _shaw_desc("SH016", 33.971952)),
    Station("SIM-SHAW-SH018", 33.964338, -80.463149, _shaw_desc("SH018", 33.964338)),
    Station("SIM-SHAW-SH019", 33.988624, -80.462155, _shaw_desc("SH019", 33.988624)),
    Station("SIM-SHAW-SH020", 33.972045, -80.462618, _shaw_desc("SH020", 33.972045)),
    Station("SIM-SHAW-SH021", 33.960408, -80.491111, _shaw_desc("SH021", 33.960408)),
    Station("SIM-SHAW-SH022", 33.990671, -80.457454, _shaw_desc("SH022", 33.990671)),
    Station("SIM-SHAW-SH023", 33.992547, -80.459717, _shaw_desc("SH023", 33.992547)),
    Station("SIM-SHAW-SH025", 33.964708, -80.459982, _shaw_desc("SH025", 33.964708)),
    Station("SIM-SHAW-SH026", 33.988703, -80.463282, _shaw_desc("SH026", 33.988703)),
    Station("SIM-SHAW-SH028", 33.967981, -80.466179, _shaw_desc("SH028", 33.967981)),
    Station("SIM-SHAW-SH030", 33.974764, -80.460174, _shaw_desc("SH030", 33.974764)),
    Station("SIM-SHAW-SH090", 33.972415, -80.444362, _shaw_desc("SH090", 33.972415)),
    Station("SIM-SHAW-SH091", 33.835432, -80.492937, _shaw_desc("SH091", 33.835432)),
    Station("SIM-SHAW-SH092", 33.834149, -80.492558, _shaw_desc("SH092", 33.834149)),
    Station("SIM-SHAW-SH095", 28.160118, -80.666432, _patrick_outlier_desc("SH095", 28.160118)),
    Station("SIM-SHAW-SH096", 33.160231, -80.664953, _sumter_south_desc("SH096", 33.160231)),
    Station("SIM-SHAW-SH097", 28.168856, -80.657836, _patrick_outlier_desc("SH097", 28.168856)),
    Station("SIM-SHAW-SH098", 28.160711, -80.666396, _patrick_outlier_desc("SH098", 28.160711)),
)

STATION_IDS: tuple[str, ...] = tuple(s.station_id for s in STATIONS)
BY_ID: dict[str, Station] = {s.station_id: s for s in STATIONS}


def short_id(station_id: str) -> str:
    """Return the original Argos suffix (``SH000``) given a SIM-prefixed
    station id (``SIM-SHAW-SH000``). PKI secret names + the
    ``out_pubkeys/`` files key on the short form."""
    if station_id.startswith("SIM-SHAW-"):
        return station_id[len("SIM-SHAW-"):]
    return station_id
