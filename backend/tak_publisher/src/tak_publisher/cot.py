from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from xml.etree.ElementTree import Element, SubElement, tostring

from .config import settings

_NULL_HAE = "9999999.0"
_NULL_ERR = "9999999"


@dataclass
class CotEvent:
    uid: str
    type: str
    time: datetime
    start: datetime
    stale: datetime
    lat: float
    lon: float
    hae: float | None
    ce: float | None
    le: float | None
    callsign: str
    group_name: str
    remarks: str

    def to_xml_bytes(self) -> bytes:
        event = Element(
            "event",
            attrib={
                "version": "2.0",
                "uid": self.uid,
                "type": self.type,
                "time": _iso(self.time),
                "start": _iso(self.start),
                "stale": _iso(self.stale),
                "how": settings.cot_how,
            },
        )
        SubElement(
            event,
            "point",
            attrib={
                "lat": f"{self.lat:.6f}",
                "lon": f"{self.lon:.6f}",
                "hae": f"{self.hae:.1f}" if self.hae is not None else _NULL_HAE,
                "ce": f"{self.ce:.1f}" if self.ce is not None else _NULL_ERR,
                "le": f"{self.le:.1f}" if self.le is not None else _NULL_ERR,
            },
        )
        detail = SubElement(event, "detail")
        SubElement(detail, "contact", attrib={"callsign": self.callsign})
        SubElement(detail, "__group", attrib={"name": self.group_name, "role": "Team Member"})
        remarks_el = SubElement(detail, "remarks")
        remarks_el.text = self.remarks
        return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + tostring(event)


def build_event(detection: dict) -> CotEvent | None:
    """Convert a detection event dict (from Pub/Sub) into a CotEvent.

    Returns None if the detection lacks a usable device location — without a
    point we can't place a marker on the TAK display.
    """
    location = detection.get("device_location")
    if not location or location.get("latitude") is None or location.get("longitude") is None:
        return None

    detection_id = detection.get("detection_id", "")
    device_id = detection.get("device_id", "unknown")
    site_label = detection.get("site_label") or ""

    avg = float(detection.get("average_score", 0.0))
    peak = float(detection.get("peak_score", 0.0))
    model = detection.get("model", {}) or {}
    model_name = str(model.get("name", ""))
    model_version = str(model.get("version", ""))

    detected_at_ms = int(
        detection.get("last_frame_timestamp_ms")
        or detection.get("published_at_ms")
        or 0
    )
    detected_at = (
        datetime.fromtimestamp(detected_at_ms / 1000, tz=timezone.utc)
        if detected_at_ms
        else datetime.now(tz=timezone.utc)
    )
    stale_at = detected_at + timedelta(seconds=settings.cot_stale_seconds)

    callsign = device_id

    remarks_parts = [
        f"Drone audio detection from {device_id}",
        f"avg={avg:.2f} peak={peak:.2f}",
        f"model={model_name}/{model_version}",
    ]
    if site_label:
        remarks_parts.append(f"site={site_label}")
    if detection.get("frames_over_threshold") is not None:
        remarks_parts.append(
            f"frames_over_threshold={detection['frames_over_threshold']}/"
            f"{detection.get('window_frames', '?')}"
        )

    return CotEvent(
        uid=f"{settings.cot_uid_prefix}.{detection_id or device_id}",
        type=settings.cot_event_type,
        time=detected_at,
        start=detected_at,
        stale=stale_at,
        lat=float(location["latitude"]),
        lon=float(location["longitude"]),
        hae=_safe_float(location.get("altitude_m")),
        ce=_safe_float(location.get("accuracy_m")),
        le=None,
        callsign=callsign,
        group_name=settings.cot_group_name,
        remarks=" | ".join(remarks_parts),
    )


def _safe_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
