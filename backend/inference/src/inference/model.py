from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
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
    subtype_label: str = ""
    subtype_confidence: float = 0.0
    subtype_probs: dict[str, float] = field(default_factory=dict)


class YAMNetModel:
    """YAMNet feature extractor + two trained ERAU dense heads.

    YAMNet (frozen, from TF Hub) maps 16 kHz mono float32 audio to a
    (frames, 1024) embedding tensor. We mean-pool over the time axis and
    feed the result through:
      - the binary head -> drone probability (drives detection)
      - the subtype head -> distribution over
          {matrice, mavic3, mavicmini, no_drone} (characterizes which
          drone model is present once a detection fires)

    YAMNet's native 521-class AudioSet scores are kept as a diagnostic
    side channel (``auxiliary_score`` and ``class_scores``).

    Provenance of the dense heads:
      models/drone_classifier_binary.keras    (test accuracy 95.2%, F1 93.2%)
      models/drone_classifier_subtype.keras   (test accuracy 95.1%, F1 macro 95.3%)
    """

    def __init__(self) -> None:
        self._yamnet = None
        self._classifier = None
        self._subtype_classifier = None
        self._subtype_labels: list[str] = []
        self._class_names: list[str] = []
        self._auxiliary_indices: list[int] = []

    def load(self) -> None:
        if (
            self._yamnet is not None
            and self._classifier is not None
            and self._subtype_classifier is not None
        ):
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
                f"Binary dense classifier not found at {head_path}."
            )
        log.info("dense_head_loading", path=str(head_path))
        self._classifier = tf.keras.models.load_model(head_path)

        subtype_path = Path(settings.subtype_classifier_path)
        labels_path = Path(settings.subtype_labels_path)
        if not subtype_path.is_file():
            raise FileNotFoundError(
                f"Subtype dense classifier not found at {subtype_path}."
            )
        if not labels_path.is_file():
            raise FileNotFoundError(
                f"Subtype labels not found at {labels_path}."
            )
        log.info("subtype_head_loading", path=str(subtype_path))
        self._subtype_classifier = tf.keras.models.load_model(subtype_path)
        self._subtype_labels = json.loads(labels_path.read_text())

        log.info(
            "yamnet_loaded",
            num_classes=len(self._class_names),
            auxiliary_indices=self._auxiliary_indices,
            dense_head_params=int(self._classifier.count_params()),
            subtype_head_params=int(self._subtype_classifier.count_params()),
            subtype_labels=self._subtype_labels,
        )

    def infer_pcm16(self, pcm16_bytes: bytes, sample_rate_hz: int) -> FrameScore:
        if (
            self._yamnet is None
            or self._classifier is None
            or self._subtype_classifier is None
        ):
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
        subtype_logits = self._subtype_classifier(pooled, training=False).numpy()[0]
        subtype_probs = {
            label: float(p)
            for label, p in zip(self._subtype_labels, subtype_logits.tolist())
        }
        top_idx = int(np.argmax(subtype_logits))
        subtype_label = self._subtype_labels[top_idx]
        subtype_confidence = float(subtype_logits[top_idx])

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
            subtype_label=subtype_label,
            subtype_confidence=subtype_confidence,
            subtype_probs=subtype_probs,
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
