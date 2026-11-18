"""Real-time visual signals per recorded segment: face, facial expression, body movement.

* Face: YuNet (OpenCV) on 640-px frames, 2 frames per second.
* Expression: FER+ (ONNX Model Zoo, MIT licence) on the 64x64 grey face crop, giving
  probabilities for neutral, happiness, surprise, sadness, anger, disgust, fear and contempt.
* Body language: frame-to-frame motion inside an enlarged face box (head, shoulders, hands).
Missing models simply disable the signal; detection then relies on the other signals.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading

import cv2
import numpy as np

from .layout import FaceDetector

log = logging.getLogger(__name__)

FER_PATH = os.environ.get("FER_MODEL", "/opt/models/emotion-ferplus-8.onnx")
EMOTIONS = ["neutral", "happiness", "surprise", "sadness", "anger", "disgust", "fear", "contempt"]
KEY_INTERVAL = 2.0   # seconds between keyframes on Twitch/Kick/YouTube live streams
W = 640

_local = threading.local()


def _models():
    if not hasattr(_local, "det"):
        _local.det = FaceDetector()
        _local.fer = None
        if os.path.exists(FER_PATH):
            try:
                _local.fer = cv2.dnn.readNetFromONNX(FER_PATH)
            except cv2.error as e:
                log.warning("FER+ model could not be loaded: %s", e)
    return _local.det, _local.fer


def available() -> bool:
    return os.path.exists(FER_PATH)


def emotions(fer, face_gray: np.ndarray) -> dict:
    img = cv2.resize(face_gray, (64, 64)).astype(np.float32).reshape(1, 1, 64, 64)
    fer.setInput(img)
    z = fer.forward().reshape(-1)
    p = np.exp(z - z.max())
    p /= p.sum()
    return {e: float(v) for e, v in zip(EMOTIONS, p)}


cv2.setNumThreads(1)


def analyze_segment(path: str, state: dict | None = None) -> list[dict]:
    """[{t, face, happy, surprise, anger, fear, sad, expr, body}] for frames at 2 fps."""
    det, fer = _models()
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", path], capture_output=True, text=True,
                           timeout=20).stdout.strip().splitlines()
    probe = probe[0].split(",") if probe else []
    if len(probe) < 2:
        return []
    w, h = int(probe[0]), int(probe[1])
    hh = int(round(h * W / w / 2) * 2)
    # Keyframes only: ~10x cheaper than decoding every frame of a 1080p60 stream, which keeps
    # real-time analysis of many simultaneous streams affordable (Twitch/Kick: 1 keyframe / 2 s).
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-skip_frame", "nokey", "-i", path,
                          "-vf", f"scale={W}:{hh}", "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                         capture_output=True, timeout=60).stdout
    n = len(raw) // (W * hh * 3)
    out = []
    state = state if state is not None else {}
    prev_box = state.get("prev_box")
    for i in range(n):
        frame = np.frombuffer(raw[i * W * hh * 3:(i + 1) * W * hh * 3], np.uint8).reshape(hh, W, 3)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = [b for b, s in det.detect(frame) if s >= 0.7 and b.w >= 20]
        rec = {"t": i * KEY_INTERVAL, "face": 0.0}
        if faces:
            b = max(faces, key=lambda b: b.w * b.h)
            x0, y0 = int(max(0, b.x)), int(max(0, b.y))
            x1, y1 = int(min(W, b.x + b.w)), int(min(hh, b.y + b.h))
            rec["face"] = 1.0
            if fer is not None and x1 - x0 > 8 and y1 - y0 > 8:
                e = emotions(fer, gray[y0:y1, x0:x1])
                rec.update(happy=e["happiness"], surprise=e["surprise"], anger=e["anger"] + e["disgust"],
                           fear=e["fear"], sad=e["sadness"], expr=1.0 - e["neutral"])
            # body language: movement in a box 2.5x the face (head, shoulders, hands)
            bx0, by0 = int(max(0, b.cx - b.w * 1.25)), int(max(0, b.cy - b.h * 1.0))
            bx1, by1 = int(min(W, b.cx + b.w * 1.25)), int(min(hh, b.cy + b.h * 2.0))
            region = cv2.resize(gray[by0:by1, bx0:bx1], (48, 48)).astype(np.float32)
            if prev_box is not None:
                rec["body"] = float(np.mean(np.abs(region - prev_box)))
            prev_box = region
        else:
            prev_box = None
        out.append(rec)
    state["prev_box"] = prev_box  # body movement is measured across keyframes of consecutive segments
    return out
