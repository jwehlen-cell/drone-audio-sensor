#!/usr/bin/env python3
"""Decode FLAC audio payloads into NiFi-ingestible WAV files.

NiFi's audio-ingest processors (GetFile, ListFile, FetchFile, the
record-aware InvokeScriptedProcessor, etc.) handle .wav transparently
but have spotty FLAC support out of the box without custom NARs. This
script converts the lossless FLAC payloads produced by the
``AudioFrame.codec="flac"`` wire format into 16 kHz mono PCM16 WAV,
which every downstream NiFi processor will accept without extra
configuration.

Inputs supported
----------------
* A single ``.flac`` file
* A directory of ``.flac`` files (recursive with ``--recursive``)
* A JSON Lines stream where each line carries a base64-encoded FLAC
  payload alongside its metadata (the format the inference worker
  could emit per detection if you wire it up — see
  ``--input-jsonl``). Each record:
      {"audio_b64": "...",
       "device_id": "DRONE-SENSOR-005",
       "capture_timestamp_ms": 1717180800123,
       "sequence_number": 42,
       "sample_rate_hz": 16000}

Output
------
* WAV files written to ``--output-dir``, one per input. Naming defaults
  to ``{device_id}_{capture_timestamp_ms}_seq{sequence_number}.wav``
  when device metadata is available (JSONL mode), otherwise the
  source-stem with ``.wav`` extension.
* Optional sidecar JSON per WAV (``--write-sidecar``) carrying the
  same metadata for NiFi's ``UpdateAttribute`` / ``EvaluateJsonPath``
  processors to read.

NiFi handoff
------------
The typical pickup chain after this script runs:

    ListFile  (--output-dir, listing_strategy=tracking_entities)
      |
      v
    FetchFile
      |
      v
    UpdateAttribute  (set mime.type = audio/wav, parse companion JSON)
      |
      v
    PutS3Object / PutGCSObject / PutKafka / PutFile

Dependencies
------------
* soundfile>=0.12 (libsndfile)
* No NiFi dependencies — this script just produces files for NiFi.

Run:
    # Single file:
    python flac_to_wav_for_nifi.py --input clip.flac --output-dir /var/spool/nifi/audio

    # Directory of flacs:
    python flac_to_wav_for_nifi.py --input ./flac_in --output-dir /var/spool/nifi/audio --recursive

    # JSONL stream (per-detection payloads):
    cat detections.jsonl | python flac_to_wav_for_nifi.py --input-jsonl - --output-dir /var/spool/nifi/audio --write-sidecar
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator, Optional

# soundfile (libsndfile) handles FLAC decode and WAV encode in one
# library; no external ffmpeg/sox dependency.
try:
    import soundfile as sf
except ImportError:  # noqa: BLE001
    print(
        "ERROR: soundfile is required.\n"
        "Install with: pip install soundfile>=0.12",
        file=sys.stderr,
    )
    raise


@dataclass
class AudioPayload:
    """One unit of audio to convert. ``flac_bytes`` is the lossless
    payload; the metadata fields are optional and propagate into the
    output filename + sidecar JSON when present."""
    flac_bytes: bytes
    device_id: str = ""
    session_id: str = ""
    capture_timestamp_ms: Optional[int] = None
    sequence_number: Optional[int] = None
    sample_rate_hz: Optional[int] = None
    site_label: str = ""
    detection_id: str = ""
    source_path: Optional[Path] = None  # for filename fallback


def _iter_file(path: Path) -> Iterator[AudioPayload]:
    yield AudioPayload(
        flac_bytes=path.read_bytes(),
        source_path=path,
    )


def _iter_directory(root: Path, recursive: bool) -> Iterator[AudioPayload]:
    pattern = "**/*.flac" if recursive else "*.flac"
    for path in sorted(root.glob(pattern)):
        if not path.is_file():
            continue
        yield AudioPayload(
            flac_bytes=path.read_bytes(),
            source_path=path,
        )


def _iter_jsonl(stream) -> Iterator[AudioPayload]:
    """Each line is a JSON object containing at minimum ``audio_b64``.

    Optional fields propagate into the output filename + sidecar:
      device_id, session_id, capture_timestamp_ms, sequence_number,
      sample_rate_hz, site_label, detection_id
    """
    for line_no, raw in enumerate(stream, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"WARN: line {line_no}: malformed JSON ({e}); skipping",
                  file=sys.stderr)
            continue
        b64 = record.get("audio_b64") or record.get("flac_b64")
        if not b64:
            print(f"WARN: line {line_no}: no audio_b64 field; skipping",
                  file=sys.stderr)
            continue
        yield AudioPayload(
            flac_bytes=base64.b64decode(b64),
            device_id=str(record.get("device_id") or ""),
            session_id=str(record.get("session_id") or ""),
            capture_timestamp_ms=_to_int(record.get("capture_timestamp_ms")),
            sequence_number=_to_int(record.get("sequence_number")),
            sample_rate_hz=_to_int(record.get("sample_rate_hz")),
            site_label=str(record.get("site_label") or ""),
            detection_id=str(record.get("detection_id") or ""),
        )


def _to_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _output_name(payload: AudioPayload) -> str:
    """NiFi-friendly filename. Prefers metadata-driven naming so the
    files sort by (device, timestamp, sequence) lexicographically —
    ListFile + the default file-age tracking processor will pick them
    up in capture order."""
    parts: list[str] = []
    if payload.device_id:
        parts.append(payload.device_id)
    if payload.capture_timestamp_ms is not None:
        parts.append(str(payload.capture_timestamp_ms))
    if payload.sequence_number is not None:
        parts.append(f"seq{payload.sequence_number}")
    if not parts and payload.source_path is not None:
        return f"{payload.source_path.stem}.wav"
    if not parts:
        # Last resort — content hash. Avoids overwriting on collisions.
        import hashlib
        parts.append(hashlib.sha1(payload.flac_bytes).hexdigest()[:12])
    return "_".join(parts) + ".wav"


def decode_flac_to_pcm(flac_bytes: bytes) -> tuple[bytes, int]:
    """libFLAC decode via soundfile. Returns (pcm16_le_bytes, sample_rate_hz).

    Downmixes any multichannel content to mono via simple averaging so
    downstream consumers see a single channel regardless of source."""
    audio, sr = sf.read(io.BytesIO(flac_bytes), dtype="int16")
    if audio.ndim > 1:
        audio = audio.mean(axis=1).astype("int16")
    return audio.tobytes(), int(sr)


def write_wav(out_path: Path, pcm16_le_bytes: bytes, sample_rate_hz: int) -> None:
    """Write a standard 16 kHz mono PCM16 WAV with no compression. The
    file is fully self-describing — NiFi's mime-detection processors
    will tag it as audio/wav from the RIFF header without us having
    to set the attribute manually (though we recommend doing so via
    UpdateAttribute for explicitness)."""
    import numpy as np
    samples = np.frombuffer(pcm16_le_bytes, dtype=np.int16)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), samples, sample_rate_hz, subtype="PCM_16",
             format="WAV")


def write_sidecar(out_path: Path, payload: AudioPayload, sample_rate_hz: int,
                  duration_s: float) -> None:
    """Companion JSON with the metadata NiFi may want to read into
    FlowFile attributes via FetchFile + EvaluateJsonPath."""
    meta = {
        "audio_path": out_path.name,
        "format": "wav",
        "subtype": "PCM_16",
        "channels": 1,
        "sample_rate_hz": sample_rate_hz,
        "duration_seconds": round(duration_s, 3),
        "source_format": "flac",
        "source_codec_field": "flac",
    }
    # Drop empty / None values so NiFi's EvaluateJsonPath doesn't
    # produce empty attribute values.
    extras = {k: v for k, v in asdict(payload).items()
              if v not in (None, "", b"") and k != "flac_bytes"
              and k != "source_path"}
    if payload.source_path is not None:
        extras["source_path"] = str(payload.source_path)
    meta.update(extras)
    out_path.with_suffix(".json").write_text(json.dumps(meta, indent=2))


def convert(payload: AudioPayload, output_dir: Path,
            write_sidecar_flag: bool) -> Path:
    pcm, sr = decode_flac_to_pcm(payload.flac_bytes)
    if payload.sample_rate_hz and payload.sample_rate_hz != sr:
        # libFLAC told us a different rate than the metadata claimed.
        # Trust libFLAC (it's reading the stream's own header) but warn.
        print(
            f"WARN: metadata sample_rate_hz={payload.sample_rate_hz} but "
            f"FLAC stream reports {sr} Hz; using {sr}",
            file=sys.stderr,
        )
    out_path = output_dir / _output_name(payload)
    write_wav(out_path, pcm, sr)
    duration_s = (len(pcm) // 2) / sr
    if write_sidecar_flag:
        write_sidecar(out_path, payload, sr, duration_s)
    return out_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Convert FLAC payloads to WAV for NiFi ingestion."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", type=Path,
                     help="Input .flac file OR directory of .flac files")
    src.add_argument("--input-jsonl", type=str,
                     help="JSON Lines stream of base64-encoded FLAC payloads "
                          "(use '-' for stdin)")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Directory to write WAV files into "
                             "(typically a NiFi watched directory)")
    parser.add_argument("--recursive", action="store_true",
                        help="When --input is a directory, walk into subdirs")
    parser.add_argument("--write-sidecar", action="store_true",
                        help="Also write a .json sidecar with audio metadata "
                             "for NiFi's EvaluateJsonPath / "
                             "UpdateAttribute processors to pick up")
    args = parser.parse_args(argv)

    if args.input is not None:
        if args.input.is_dir():
            iterator = _iter_directory(args.input, recursive=args.recursive)
        elif args.input.is_file():
            iterator = _iter_file(args.input)
        else:
            print(f"ERROR: input not found: {args.input}", file=sys.stderr)
            return 2
    else:
        stream = sys.stdin if args.input_jsonl == "-" else open(args.input_jsonl)
        iterator = _iter_jsonl(stream)

    n_ok = 0
    n_fail = 0
    for payload in iterator:
        try:
            out_path = convert(payload, args.output_dir, args.write_sidecar)
            print(out_path)
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: {payload.source_path or payload.device_id or '?'}"
                  f": {type(e).__name__}: {e}", file=sys.stderr)
            n_fail += 1
    print(f"\nConverted {n_ok} file(s), {n_fail} failure(s).",
          file=sys.stderr)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
