"""Choose the clip's opening and ending, and remove dead air.

All times here are seconds from the start of the extracted source window ("media time").
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .transcribe import Word

STEP = 0.5  # resolution of the interest curve


@dataclass
class CutPlan:
    start: float
    end: float
    peak: float
    keep: list[tuple[float, float]]   # intervals of the source window that stay in the clip
    interest: np.ndarray              # per STEP over the whole source window

    @property
    def duration(self) -> float:
        return sum(b - a for a, b in self.keep)


def _norm(x: np.ndarray) -> np.ndarray:
    if len(x) == 0:
        return x
    lo, hi = np.percentile(x, 10), np.percentile(x, 98)
    return np.clip((x - lo) / max(hi - lo, 1e-6), 0, 1)


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) < n or n <= 1:
        return x
    k = np.ones(n) / n
    return np.convolve(x, k, mode="same")


def interest_curve(duration: float, chat: np.ndarray, audio: np.ndarray, motion: np.ndarray,
                   words: list[Word]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """All inputs are already resampled to STEP. Returns (interest, speech, audio_norm)."""
    n = int(np.ceil(duration / STEP))
    def fit(a):
        a = np.asarray(a, dtype=np.float32)[:n]
        return np.pad(a, (0, n - len(a))) if len(a) < n else a
    chat, audio, motion = fit(chat), fit(audio), fit(motion)
    speech = np.zeros(n, dtype=np.float32)
    for w in words:
        speech[int(w.start / STEP): int(np.ceil(w.end / STEP))] = 1
    an = _norm(audio)
    interest = 0.5 * _norm(chat) + 0.3 * an + 0.1 * _norm(motion) + 0.1 * speech
    return _smooth(interest, 3), speech, an


def boundary_candidates(words: list[Word], segments: list[tuple[float, float, str]]) -> list[float]:
    """Natural places to start a clip: sentence starts and pauses between words."""
    c = {round(s[0], 2) for s in segments}
    for a, b in zip(words, words[1:]):
        if b.start - a.end >= 0.35 or a.text.rstrip().endswith((".", "!", "?")):
            c.add(round(b.start - 0.05, 2))
    return sorted(c)


def plan_cut(duration: float, ev0: float, ev1: float, peak_hint: float,
             chat: np.ndarray, audio: np.ndarray, motion: np.ndarray,
             words: list[Word], segments: list[tuple[float, float, str]],
             min_sec: float, max_sec: float, remove_silence: bool = True,
             min_silence: float = 1.2, keep_silence: float = 0.3,
             scene_cuts: list[float] | None = None, ideal: tuple[float, float] = (20, 40),
             preferred_max: float = 45, hard_max: float = 60) -> CutPlan:
    """min_sec: shortest clip we will make at all; ideal: target range (we only extend toward
    ideal[0] with real context - an earlier sentence - never with filler); preferred_max: trim
    toward this; hard_max: never exceeded."""
    max_sec = min(max_sec, hard_max)
    interest, speech, an = interest_curve(duration, chat, audio, motion, words)
    n = len(interest)
    t = np.arange(n) * STEP

    # Peak: strongest point around the detected event.
    lo, hi = max(0, int((ev0 - 5) / STEP)), min(n, int((ev1 + 5) / STEP) + 1)
    peak = float(t[lo + int(np.argmax(interest[lo:hi]))]) if hi > lo else peak_hint
    pv = float(interest[int(peak / STEP)]) if n else 0.0

    # Ending: once the reaction has died down for ~2 s (and the sentence has finished).
    end = min(duration, max(ev1, peak) + 4)
    i = int((peak + 3) / STEP)
    quiet_needed = int(2.0 / STEP)
    run = 0
    while i < n:
        run = run + 1 if interest[i] < 0.35 * pv else 0
        if run >= quiet_needed:
            end = t[i - quiet_needed + 1] + 1.0
            break
        i += 1
    else:
        end = duration
    end = max(end, ev1)
    # never stop mid-word; finish the sentence only if it ends within 4 s (no chaining through
    # continuous speech - that used to drag the ending far past the moment)
    for w in words:
        if w.start < end < w.end:
            end = w.end + 0.15
            break
    for s0, s1, _ in segments:
        if s0 < end < s1 and s1 - end <= 4:
            end = s1 + 0.3
            break
    end = min(end, duration)

    # Opening: a sentence start/pause roughly 6-14 s before the peak, favouring a lively start.
    cands = [c for c in boundary_candidates(words, segments) if max(0, peak - 30) <= c <= peak - 1.5]
    cands.append(max(0.0, peak - 10))
    def cost(c: float) -> float:
        ctx = peak - c
        hook = float(np.mean(interest[int(c / STEP): int((c + 3) / STEP) + 1])) if n else 0
        talking = float(np.mean(speech[int(c / STEP): int((c + 2) / STEP) + 1])) if n else 0
        return abs(ctx - 9) / 6 - 1.2 * hook - 0.5 * talking
    start = min(cands, key=cost)

    # Scene detection: if a hard scene change sits just after the opening / just before the
    # ending, cut exactly on it so the clip doesn't start or end with a flash of another shot.
    for c in scene_cuts or []:
        if start < c <= start + 1.0 and c <= peak - 1.5:
            start = c
        if end - 1.0 <= c < end and c >= peak + 1.5:
            end = c - 0.04

    # Length rules: ideal 20-40 s, preferred max 45 s, absolute max 60 s. Short moments stay short
    # unless an earlier sentence gives real context; never pad with irrelevant footage.
    cands_all = boundary_candidates(words, segments)
    if end - start < ideal[0]:
        earlier = [c for c in cands_all if end - ideal[0] - 4 <= c < start and peak - c <= 20]
        if earlier:                       # one more sentence of context, only if it exists
            start = max(earlier[0], end - ideal[1])
    if end - start < min_sec:             # extremely short: the minimum we accept
        start = max(0.0, end - min_sec)
    limit = preferred_max if end - start > preferred_max else max_sec
    if end - start > limit:
        later = [c for c in cands_all if end - limit <= c <= peak - 3]
        if later:
            start = later[0]
        else:
            # trimming the opening would lose the moment's context: shorten the ending instead
            start = min(max(start, end - limit), max(0.0, peak - 3))
        if end - start > limit:           # the reaction itself is long: end at a sentence end
            ends = [s1 for _, s1, _ in segments if peak + 2 <= s1 <= start + limit]
            end = (ends[-1] + 0.3) if ends else start + limit
    if end - start > hard_max:
        end = start + hard_max
    start, end = max(0.0, start), min(duration, end)
    if start > peak - 1:                  # the moment itself must always be in the clip
        start = max(0.0, peak - 3)
        end = min(end, start + hard_max)

    keep = [(start, end)]
    if remove_silence:
        mn = _norm(np.asarray(motion, dtype=np.float32)[:n]) if len(motion) else np.zeros(n)
        if len(mn) < n:
            mn = np.pad(mn, (0, n - len(mn)))
        trimmed = _remove_dead_air(start, end, t, speech, an, interest, min_silence, keep_silence, peak, mn)
        # removing dead air must not push the clip under the minimum length
        if sum(b - a for a, b in trimmed) >= min(min_sec, end - start):
            keep = trimmed
    return CutPlan(start, end, peak, keep, interest)


def _remove_dead_air(start, end, t, speech, audio_n, interest, min_silence, keep_silence, peak, motion_n=None):
    """Cut dead stretches: silence (no speech, quiet, nothing happening) and waiting/loading
    screens (no speech, a static picture, no chat reaction - even if music is playing)."""
    dead = (speech < 0.5) & (audio_n < 0.15) & (interest < 0.2)
    if motion_n is not None and len(motion_n) == len(speech):
        dead |= (speech < 0.5) & (motion_n < 0.03) & (interest < 0.25)
    keep: list[tuple[float, float]] = []
    cur = start
    i0 = int(start / STEP)
    i1 = min(len(t), int(end / STEP))
    i = i0
    while i < i1:
        if dead[i]:
            j = i
            while j < i1 and dead[j]:
                j += 1
            a, b = t[i], t[j - 1] + STEP
            # never remove the moment itself
            if b - a >= min_silence and not (a <= peak <= b):
                half = keep_silence / 2
                if a + half > cur:
                    keep.append((cur, a + half))
                cur = b - half
            i = j
        else:
            i += 1
    if end - cur > 0.2:
        keep.append((cur, end))
    return [(round(a, 3), round(b, 3)) for a, b in keep if b - a > 0.1]


def scene_cuts(motion: np.ndarray, fps: float) -> list[float]:
    """Hard cuts from the per-frame motion series (mean abs difference of tiny gray frames)."""
    if len(motion) < 3:
        return []
    thr = max(30.0, 5 * float(np.median(motion)))
    return [round(i / fps, 2) for i in range(1, len(motion)) if motion[i] > thr]


def map_time(t: float, keep: list[tuple[float, float]]) -> float | None:
    """Source-window time -> output clip time (None if the moment was removed)."""
    acc = 0.0
    for a, b in keep:
        if a <= t <= b:
            return acc + (t - a)
        acc += b - a
    return None
