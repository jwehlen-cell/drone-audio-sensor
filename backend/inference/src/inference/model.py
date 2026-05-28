from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

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
    """YAMNet feature extractor + a trained ERAU dense head for drone detection.

    YAMNet (frozen, from TF Hub) maps 16 kHz mono float32 audio to a
    (frames, 1024) embedding tensor. We mean-pool over the time axis and feed
    the result through a small dense classifier trained on the Embry-Riddle
    YAMNet drone-embedding dataset (DOI 10.17632/5dmcszvym4.3). The dense
    head outputs a sigmoid drone probability in [0, 1].

    YAMNet's native 521-class AudioSet scores are kept as a diagnostic side
    channel (``auxiliary_score`` and ``class_scores``) so downstream Pub/Sub
    consumers can still see Helicopter/Aircraft/Propeller signals when a
    detection fires.

    Provenance of the dense head weights:
      models/drone_classifier_binary.keras
      Trained on TF 2.20; loads on the container's TF 2.16 runtime.
      Test accuracy 95.2%, F1 93.2% on a 1,822-sample stratified holdout.
    """

    _DRONE_FALLBACK_INDEX = 478  # AudioSet "Drone" — used only if class map parse fails

    def __init__(self) -> None:
        self._yamnet = None
        self._classifier = None
        self._class_names: list[str] = []
        self._auxiliary_indices: list[int] = []

    def load(self) -> None:
        if self._yamnet is not None and self._classifier is not None:
            return

        log.info("yamnet_loading", handle=settings.model_handle)
        import tensorflow as tf  # type: ignore[import-untyped]
        import tensorflow_hub as hub  # type: ignore[import-untyped]

        self._yamnet = hub.load(settings.model_handle)
        self._class_names = self._load_class_names()
        self._auxiliary_indices = self._indices_for(settings.auxiliary_class_names)

        head_path = Path(settings.dense_classifier_path)
        if not head_path.is_file():
            raise FileNotFoundError(
                f"Trained dense classifier not found at {head_path}. "
                "Expected to be baked into the container under /app/models/."
            )
        log.info("dense_head_loading", path=str(head_path))
        self._classifier = tf.keras.models.load_model(head_path)

        log.info(
            "yamnet_loaded",
            num_classes=len(self._class_names),
            auxiliary_indices=self._auxiliary_indices,
            dense_head_params=int(self._classifier.count_params()),
            dense_head_output=tuple(self._classifier.output_shape),
        )

    def infer_pcm16(self, pcm16_bytes: bytes, sample_rate_hz: int) -> FrameScore:
        if self._yamnet is None or self._classifier is None:
            raise RuntimeError("Model not loaded; call load() first")
        if sample_rate_hz != 16_000:
            raise ValueError(f"YAMNet requires 16 kHz audio; got {sample_rate_hz}")

        waveform = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if waveform.size == 0:
            return FrameScore(0.0, 0.0, {})

        scores_tensor, embeddings_tensor, _spectrogram = self._yamnet(waveform)

        embeddings = embeddings_tensor.numpy()  # (T, 1024)
        if embeddings.size == 0:
            return FrameScore(0.0, 0.0, {})
        pooled = embeddings.mean(axis=0, keepdims=True).astype(np.float32)  # (1, 1024)
        # Direct __call__ is ~3-4x faster than .predict() for single-sample
        # inference; .predict() has heavy per-call overhead.
        drone_prob = float(self._classifier(pooled, training=False).numpy()[0, 0])

        scores = scores_tensor.numpy()
        per_class_mean = scores.mean(axis=0)
        aux_score = (
            float(per_class_mean[self._auxiliary_indices].max())
            if self._auxiliary_indices
            else 0.0
        )
        class_scores = {
            self._class_names[i]: float(per_class_mean[i])
            for i in self._auxiliary_indices
        }
        return FrameScore(
            drone_score=drone_prob,
            auxiliary_score=aux_score,
            class_scores=class_scores,
        )

    def _load_class_names(self) -> list[str]:
        try:
            assert self._yamnet is not None
            class_map_path = self._yamnet.class_map_path().numpy().decode("utf-8")
            with open(class_map_path) as f:
                rows = list(csv.DictReader(f))
            return [r["display_name"] for r in rows]
        except Exception as e:  # noqa: BLE001
            log.warning("yamnet_class_map_load_failed", error=str(e))
            return []

    def _indices_for(self, names: tuple[str, ...] | list[str]) -> list[int]:
        if not self._class_names:
            return []
        targets = {n.lower() for n in names}
        return [i for i, n in enumerate(self._class_names) if n.lower() in targets]
