"""Chat lexicon + per-segment audio/visual feature extraction."""
from __future__ import annotations

import re
import subprocess
from collections import Counter

import numpy as np

# Reaction categories. Patterns are matched against lowercased messages.
CATEGORIES: dict[str, re.Pattern] = {
    "laugh": re.compile(
        r"\b(l+o+l+|lmf?a+o+|kek\w*|lu+l\w*|omegalul|icant|ha(ha)+h*|xd+|pepelaugh|dead+)\b|💀|😂|🤣|😭"),
    "shock": re.compile(
        r"\b(wtf+|omf?g+|no+\s?wa+y+|na+h+|wha+t+|holy|yo+o+|bru+h+|sheesh|wow)\b|😱|😳|🤯|‼|❗"),
    "hype": re.compile(
        r"(^|\s)w+(\s|$)|\b(pog\w*|let'?s\s?go+|goat\w*|insane|cracked|clean|hype|fire)\b|🔥|🐐"),
    "fail": re.compile(r"(^|\s)(l+|f)(\s|$)|\b(rip|sadge|bozo|unlucky|ff|fail\w*|throw\w*|choke\w*)\b"),
    "tension": re.compile(
        r"\b(yikes|monka\w*|ratio|cancel+\w*|drama|cringe|awkward|chill|calm down|beef)\b|\?{2,}"),
    "clip": re.compile(r"\bclip(ped| it| that|s)?\b"),
}
REACTION = ("laugh", "shock", "hype", "fail", "tension", "clip")

_repeat = re.compile(r"(.)\1{2,}")
_ws = re.compile(r"\s+")
_squeeze = re.compile(r"(.)\1+")
_word = re.compile(r"[\w']+|[^\w\s]", re.UNICODE)


def normalize(text: str) -> str:
    t = _repeat.sub(r"\1\1", text.lower().strip())
    return _ws.sub(" ", t)[:40]


def classify(text: str) -> set[str]:
    t = normalize(text)
    return {name for name, rx in CATEGORIES.items() if rx.search(t)}


def tokens(text: str) -> list[str]:
    return [w for w in _word.findall(normalize(text)) if len(w) > 0]


def top_tokens(texts: list[str], n: int = 5) -> list[tuple[str, int]]:
    c: Counter = Counter()
    for t in texts:
        c.update(set(tokens(t)))
    for stop in ("the", "a", "i", "to", "is", "it", "and", "you", "of", "he", "that", "in", "@", ",", ".", "'"):
        c.pop(stop, None)
    return c.most_common(n)


def repetition_share(texts: list[str]) -> float:
    """Share of messages equal to the most common normalized message (emote walls, copypasta)."""
    if not texts:
        return 0.0
    c = Counter(_squeeze.sub(r"\1", normalize(t)) for t in texts)
    return c.most_common(1)[0][1] / len(texts)


# ---- audio / visual ------------------------------------------------------

AUDIO_SR = 16000
AUDIO_HOP = 0.5   # seconds per loudness value
VIDEO_FPS = 4     # motion samples per second


def segment_features(path: str) -> tuple[np.ndarray, np.ndarray]:
    """Return (loudness_db per 0.5 s, motion per 0.25 s) for a media file."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
           "-map", "0:a:0?", "-ac", "1", "-ar", str(AUDIO_SR), "-f", "s16le", "pipe:1"]
    a = subprocess.run(cmd, capture_output=True, timeout=60).stdout
    audio = np.frombuffer(a, dtype=np.int16).astype(np.float32) / 32768.0
    hop = int(AUDIO_SR * AUDIO_HOP)
    n = len(audio) // hop
    if n:
        frames = audio[: n * hop].reshape(n, hop)
        rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
        loud = 20 * np.log10(rms + 1e-9)
    else:
        loud = np.zeros(0, dtype=np.float32)
    w, h = 64, 36
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:v:0",
           "-vf", f"fps={VIDEO_FPS},scale={w}:{h},format=gray", "-f", "rawvideo", "pipe:1"]
    v = subprocess.run(cmd, capture_output=True, timeout=60).stdout
    nf = len(v) // (w * h)
    if nf >= 2:
        frames = np.frombuffer(v[: nf * w * h], dtype=np.uint8).reshape(nf, h, w).astype(np.float32)
        motion = np.concatenate([[0.0], np.mean(np.abs(np.diff(frames, axis=0)), axis=(1, 2))])
    else:
        motion = np.zeros(max(nf, 0), dtype=np.float32)
    return loud.astype(np.float32), motion.astype(np.float32)
