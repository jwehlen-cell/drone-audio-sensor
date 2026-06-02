"""Argos-UAT replay simulator roster.

33 simulated SH-* stations distributed across two clusters:

  - Shaw / Sumter, SC  (~33.9 N, -80.4..-80.5 W) — 31 stations
  - Patrick SFB, FL    (~28.16 N, -80.66 W)      —  4 stations

Names and coordinates are the public station labels used by the prod
Argos pipeline; nothing here represents any real PKI material — the
keypairs minted under these labels by ``mint_test_pki.py`` are fresh,
sandbox-only, and never touch the prod registry.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    station_id: str
    latitude: float
    longitude: float


STATIONS: tuple[Station, ...] = (
    Station("SH000", 28.160115, -80.666448),
    Station("SH002", 33.989119, -80.454027),
    Station("SH003", 33.974757, -80.445196),
    Station("SH004", 33.968807, -80.447180),
    Station("SH005", 33.985686, -80.455629),
    Station("SH006", 33.963007, -80.466037),
    Station("SH007", 33.965554, -80.445582),
    Station("SH008", 33.977136, -80.447279),
    Station("SH009", 33.990082, -80.461216),
    Station("SH010", 33.987692, -80.459884),
    Station("SH011", 33.969347, -80.441949),
    Station("SH012", 33.969537, -80.464734),
    Station("SH013", 33.965186, -80.449219),
    Station("SH014", 33.961795, -80.469965),
    Station("SH015", 33.969073, -80.443608),
    Station("SH016", 33.971952, -80.442491),
    Station("SH018", 33.964338, -80.463149),
    Station("SH019", 33.988624, -80.462155),
    Station("SH020", 33.972045, -80.462618),
    Station("SH021", 33.960408, -80.491111),
    Station("SH022", 33.990671, -80.457454),
    Station("SH023", 33.992547, -80.459717),
    Station("SH025", 33.964708, -80.459982),
    Station("SH026", 33.988703, -80.463282),
    Station("SH028", 33.967981, -80.466179),
    Station("SH030", 33.974764, -80.460174),
    Station("SH090", 33.972415, -80.444362),
    Station("SH091", 33.835432, -80.492937),
    Station("SH092", 33.834149, -80.492558),
    Station("SH095", 28.160118, -80.666432),
    Station("SH096", 33.160231, -80.664953),
    Station("SH097", 28.168856, -80.657836),
    Station("SH098", 28.160711, -80.666396),
)

STATION_IDS: tuple[str, ...] = tuple(s.station_id for s in STATIONS)
BY_ID: dict[str, Station] = {s.station_id: s for s in STATIONS}
