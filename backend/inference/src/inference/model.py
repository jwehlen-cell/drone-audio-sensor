from __future__ import annotations

import csv
import io
from dataclasses import dataclass

import numpy as np
import structlog

from .config import settings

log = structlog.get_logger(__name__)


@dataclass
class FrameScore:
    drone_score: float
    auxiliary_score: float
    class_scores: dict[str, float]


class YAMNetModel:
    """Lazy-loaded TF Hub YAMNet wrapper.

    YAMNet expects mono 16 kHz float32 in [-1.0, 1.0] and returns a (frames, 521)
    score tensor. We collapse to a single score by averaging across the internal
    frames (each 0.96s).
    """

    _DRONE_FALLBACK_INDEX = 478  # AudioSet "Drone" — used only if class map parse fails

    def __init__(self) -> None:
        self._model = None
        self._class_names: list[str] = []
        self._drone_indices: list[int] = []
        self._auxiliary_indices: list[int] = []

    def load(self) -> None:
        if self._model is not None:
            return
        log.info("yamnet_loading", handle=settings.model_handle)
        import tensorflow_hub as hub  # type: ignore[import-untyped]

        self._model = hub.load(settings.model_handle)
        self._class_names = self._load_class_names()
        self._drone_indices = self._indices_for(settings.drone_class_names)
        self._auxiliary_indices = self._indices_for(settings.auxiliary_class_names)
        log.info(
            "yamnet_loaded",
            num_classes=len(self._class_names),
            drone_indices=self._drone_indices,
            auxiliary_indices=self._auxiliary_indices,
        )

    def infer_pcm16(self, pcm16_bytes: bytes, sample_rate_hz: int) -> FrameScore:
        if self._model is None:
            raise RuntimeError("YAMNet model not loaded; call load() first")
        if sample_rate_hz != 16_000:
            raise ValueError(f"YAMNet requires 16 kHz audio; got {sample_rate_hz}")

        waveform = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if waveform.size == 0:
            return FrameScore(0.0, 0.0, {})

        scores_tensor, _embeddings, _spectrogram = self._model(waveform)
        scores = scores_tensor.numpy()
        per_class_mean = scores.mean(axis=0)
        drone_score = float(per_class_mean[self._drone_indices].max()) if self._drone_indices else 0.0
        aux_score = (
            float(per_class_mean[self._auxiliary_indices].max())
            if self._auxiliary_indices
            else 0.0
        )
        class_scores = {
            self._class_names[i]: float(per_class_mean[i])
            for i in self._drone_indices + self._auxiliary_indices
        }
        return FrameScore(
            drone_score=drone_score,
            auxiliary_score=aux_score,
            class_scores=class_scores,
        )

    def _load_class_names(self) -> list[str]:
        try:
            assert self._model is not None
            class_map_path = self._model.class_map_path().numpy().decode("utf-8")
            with open(class_map_path) as f:
                rows = list(csv.DictReader(f))
            return [r["display_name"] for r in rows]
        except Exception as e:  # noqa: BLE001
            log.warning("yamnet_class_map_load_failed", error=str(e))
            return []

    def _indices_for(self, names: tuple[str, ...] | list[str]) -> list[int]:
        if not self._class_names:
            return [self._DRONE_FALLBACK_INDEX] if any(n == "Drone" for n in names) else []
        targets = {n.lower() for n in names}
        return [i for i, n in enumerate(self._class_names) if n.lower() in targets]
