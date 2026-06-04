"""Argos-UAT SH-* sensor roster.

33 Argos sensors (live or replayed) surfaced under ARGOS-SHAW-SH###
device_ids in argosuat. Both the live-pull subscriber and the
historical replay bridge share this roster — the roster is just a
list of "real Argos sensors we relay into argosuat," not a marker of
which path is active.

Device IDs prefix the original Argos identifier with ``ARGOS-SHAW-``
to distinguish them from simulated phones (``SIM-PATRICK-001`` etc.)
and to make the upstream provenance obvious on the admin dashboard.
The Shaw label reflects the user-configured site grouping (most of
the SH-* stations sit in the Shaw AFB / Sumter SC cluster); the four
outliers near Cape Canaveral keep the SHAW prefix for consistency,
with their actual location surfaced via the description field.

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
        f"ARGOS – Shaw AFB / Sumter SC cluster, "
        f"sensor {sh_id} ({lat:.3f}°N)"
    )


def _patrick_outlier_desc(sh_id: str, lat: float) -> str:
    return (
        f"ARGOS – Patrick SFB FL outlier, "
        f"sensor {sh_id} ({lat:.3f}°N) – physically not at Shaw"
    )


def _sumter_south_desc(sh_id: str, lat: float) -> str:
    return (
        f"ARGOS – south SC outlier, "
        f"sensor {sh_id} ({lat:.3f}°N) – physically not at Shaw"
    )


STATIONS: tuple[Station, ...] = (
    Station("ARGOS-SHAW-SH000", 28.160115, -80.666448, _patrick_outlier_desc("SH000", 28.160115)),
    Station("ARGOS-SHAW-SH002", 33.989119, -80.454027, _shaw_desc("SH002", 33.989119)),
    Station("ARGOS-SHAW-SH003", 33.974757, -80.445196, _shaw_desc("SH003", 33.974757)),
    Station("ARGOS-SHAW-SH004", 33.968807, -80.447180, _shaw_desc("SH004", 33.968807)),
    Station("ARGOS-SHAW-SH005", 33.985686, -80.455629, _shaw_desc("SH005", 33.985686)),
    Station("ARGOS-SHAW-SH006", 33.963007, -80.466037, _shaw_desc("SH006", 33.963007)),
    Station("ARGOS-SHAW-SH007", 33.965554, -80.445582, _shaw_desc("SH007", 33.965554)),
    Station("ARGOS-SHAW-SH008", 33.977136, -80.447279, _shaw_desc("SH008", 33.977136)),
    Station("ARGOS-SHAW-SH009", 33.990082, -80.461216, _shaw_desc("SH009", 33.990082)),
    Station("ARGOS-SHAW-SH010", 33.987692, -80.459884, _shaw_desc("SH010", 33.987692)),
    Station("ARGOS-SHAW-SH011", 33.969347, -80.441949, _shaw_desc("SH011", 33.969347)),
    Station("ARGOS-SHAW-SH012", 33.969537, -80.464734, _shaw_desc("SH012", 33.969537)),
    Station("ARGOS-SHAW-SH013", 33.965186, -80.449219, _shaw_desc("SH013", 33.965186)),
    Station("ARGOS-SHAW-SH014", 33.961795, -80.469965, _shaw_desc("SH014", 33.961795)),
    Station("ARGOS-SHAW-SH015", 33.969073, -80.443608, _shaw_desc("SH015", 33.969073)),
    Station("ARGOS-SHAW-SH016", 33.971952, -80.442491, _shaw_desc("SH016", 33.971952)),
    Station("ARGOS-SHAW-SH018", 33.964338, -80.463149, _shaw_desc("SH018", 33.964338)),
    Station("ARGOS-SHAW-SH019", 33.988624, -80.462155, _shaw_desc("SH019", 33.988624)),
    Station("ARGOS-SHAW-SH020", 33.972045, -80.462618, _shaw_desc("SH020", 33.972045)),
    Station("ARGOS-SHAW-SH021", 33.960408, -80.491111, _shaw_desc("SH021", 33.960408)),
    Station("ARGOS-SHAW-SH022", 33.990671, -80.457454, _shaw_desc("SH022", 33.990671)),
    Station("ARGOS-SHAW-SH023", 33.992547, -80.459717, _shaw_desc("SH023", 33.992547)),
    Station("ARGOS-SHAW-SH025", 33.964708, -80.459982, _shaw_desc("SH025", 33.964708)),
    Station("ARGOS-SHAW-SH026", 33.988703, -80.463282, _shaw_desc("SH026", 33.988703)),
    Station("ARGOS-SHAW-SH028", 33.967981, -80.466179, _shaw_desc("SH028", 33.967981)),
    Station("ARGOS-SHAW-SH030", 33.974764, -80.460174, _shaw_desc("SH030", 33.974764)),
    Station("ARGOS-SHAW-SH090", 33.972415, -80.444362, _shaw_desc("SH090", 33.972415)),
    Station("ARGOS-SHAW-SH091", 33.835432, -80.492937, _shaw_desc("SH091", 33.835432)),
    Station("ARGOS-SHAW-SH092", 33.834149, -80.492558, _shaw_desc("SH092", 33.834149)),
    Station("ARGOS-SHAW-SH095", 28.160118, -80.666432, _patrick_outlier_desc("SH095", 28.160118)),
    Station("ARGOS-SHAW-SH096", 33.160231, -80.664953, _sumter_south_desc("SH096", 33.160231)),
    Station("ARGOS-SHAW-SH097", 28.168856, -80.657836, _patrick_outlier_desc("SH097", 28.168856)),
    Station("ARGOS-SHAW-SH098", 28.160711, -80.666396, _patrick_outlier_desc("SH098", 28.160711)),
)

STATION_IDS: tuple[str, ...] = tuple(s.station_id for s in STATIONS)
BY_ID: dict[str, Station] = {s.station_id: s for s in STATIONS}


def short_id(station_id: str) -> str:
    """Return the original Argos suffix (``SH000``) given an
    argosuat-prefixed station id (``ARGOS-SHAW-SH000``). PKI secret
    names + the ``out_pubkeys/`` files key on the short form. The
    legacy ``SIM-SHAW-`` prefix is also accepted so older fixtures
    keep working through the renaming window."""
    if station_id.startswith("ARGOS-SHAW-"):
        return station_id[len("ARGOS-SHAW-"):]
    if station_id.startswith("SIM-SHAW-"):
        return station_id[len("SIM-SHAW-"):]
    return station_id
