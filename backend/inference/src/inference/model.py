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
    # Max AudioSet score over the confounder classes + which class — drives the
    # confounder veto (frog/insect/vehicle/train). 0.0 when none configured.
    confounder_score: float = 0.0
    confounder_class: str = ""


# Operational category thresholds. The trigger matches
# INFERENCE_DETECTION_THRESHOLD; the lower bound of the uncertain band
# is what separates "Unknown source" (could be a drone we've never
# seen) from "no_drone" (confidently silent / not-drone-like).
DRONE_TRIGGER_DEFAULT = 0.5
UNCERTAIN_LOW_DEFAULT = 0.2


def categorize(
    drone_score: float,
    subtype_label: str,
    drone_threshold: float = DRONE_TRIGGER_DEFAULT,
    uncertain_low: float = UNCERTAIN_LOW_DEFAULT,
) -> tuple[str, str]:
    """Map (binary head, subtype head) into one of four ops categories.

    Returns (category_token, display_string).

      known_drone   : binary fires AND subtype matches a trained drone
                      class. ``display_string`` is the raw subtype
                      token (e.g. ``"mavicmini"``) — callers map it to
                      maker + model at the UI layer.
      unknown_drone : binary fires AND subtype top is ``"no_drone"``
                      (binary head detected a drone, but the
                      characterizer doesn't recognize it as any of the
                      trained models). Operationally: "drone here, type
                      unknown".
      unknown_source: binary doesn't fire but drone_score is above
                      ``uncertain_low`` — possible unfamiliar drone
                      (out-of-distribution input).
      no_drone      : drone_score below ``uncertain_low`` (confident
                      not-a-drone).

    Detection events only fire when ``drone_score >= drone_threshold``,
    so build_detection() never sees the lower two categories. The
    helper is exposed at frame level for telemetry/operations.
    """
    if drone_score >= drone_threshold:
        if subtype_label == "no_drone":
            return "unknown_drone", "Unknown drone"
        return "known_drone", subtype_label
    if drone_score >= uncertain_low:
        return "unknown_source", "Unknown source"
    return "no_drone", "no_drone"


class YAMNetModel:
    """YAMNet feature extractor + two trained dense heads.

    YAMNet (frozen, from TF Hub) maps 16 kHz mono float32 audio to a
    (frames, 1024) embedding tensor. We mean-pool over the time axis and
    feed the result through:
      - the binary head -> drone probability (drives detection)
      - the subtype head -> distribution over
          {bebop, mambo, matrice, mavic3, mavicmini, no_drone}
          (characterizes which drone model is present once a detection
          fires)

    YAMNet's native 521-class AudioSet scores are kept as a diagnostic
    side channel (``auxiliary_score`` and ``class_scores``).

    Training data: ERAU YAMNet drone-embedding dataset (matrice / mavic3 /
    mavicmini) plus saraalemadi/DroneAudioDataset (bebop / mambo + ESC-50
    environmental negatives) and 32 demo clips from mackenzie-jane/
    drone-visualization (binary positives only). The authoritative label
    list lives in the sidecar JSON loaded at startup.

    Provenance of the dense heads:
      models/drone_classifier_binary.keras    (test accuracy 95.3%, F1 93.6%)
      models/drone_classifier_subtype.keras   (test accuracy 93.6%, F1 macro 91.6%)
    """

    def __init__(self) -> None:
        self._yamnet = None
        self._classifier = None
        self._subtype_classifier = None
        self._subtype_labels: list[str] = []
        self._class_names: list[str] = []
        self._auxiliary_indices: list[int] = []
        self._confounder_indices: list[int] = []

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
        self._confounder_indices = self._indices_for(settings.confounder_class_names)

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

    # YAMNet's internal window is 0.96 s (15,360 samples at 16 kHz) with
    # a 0.48 s hop. Inputs shorter than that produce zero embeddings;
    # we pad with trailing silence so a short frame still produces one
    # score. Longer inputs are handled natively — YAMNet emits multiple
    # embeddings which we mean-pool below — so we don't cap the upper
    # length here.
    _YAMNET_MIN_SAMPLES = 15_360

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
        if waveform.size < self._YAMNET_MIN_SAMPLES:
            waveform = np.pad(
                waveform,
                (0, self._YAMNET_MIN_SAMPLES - waveform.size),
            )

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
            for i in self._auxiliary_indices + self._confounder_indices
        }
        # Confounder veto scores: the Shaw field audio is extremely low-amplitude
        # (peak ~5e-4), so YAMNet's AudioSet head collapses to "Silence" on the
        # raw waveform and can't name the confounder. A peak-normalized second
        # pass recovers it (e.g. SH010's distant road rumble -> Vehicle ~0.6).
        # This pass is veto-only; the binary/subtype heads stay on the
        # un-normalized embeddings they were trained on. It can only suppress an
        # already-firing frame, never create a detection.
        confounder_score = 0.0
        confounder_class = ""
        if self._confounder_indices and settings.confounder_veto_enabled:
            peak = float(np.abs(waveform).max())
            norm = (waveform / peak * 0.95).astype(np.float32) if peak > 1e-6 else waveform
            norm_scores, _e, _s = self._yamnet(norm)
            norm_mean = norm_scores.numpy().mean(axis=0)
            ci = int(max(self._confounder_indices, key=lambda i: norm_mean[i]))
            confounder_score = float(norm_mean[ci])
            confounder_class = self._class_names[ci]
        return FrameScore(
            drone_score=drone_prob,
            auxiliary_score=aux_score,
            class_scores=class_scores,
            confounder_score=confounder_score,
            confounder_class=confounder_class,
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
