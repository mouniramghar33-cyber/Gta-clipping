"""Viral moment detection on rolling chat windows, relative to the streamer's own baseline.

Every `step_sec` the detector looks at the last `window_sec` of chat and compares it with
the streamer's normal activity (robust median/MAD of 10 s bins over `baseline_minutes`).
Audio can make detection easier, but loud audio alone never creates a moment.
"""
from __future__ import annotations

import bisect
import statistics
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import signals


@dataclass
class ChatMsg:
    ts: float
    user: str
    text: str
    cats: frozenset


@dataclass
class Window:
    t0: float
    t1: float
    messages: int
    users: int
    hype_share: float
    cats: dict
    repetition: float
    baseline: float
    ratio: float
    z: float
    audio_spike_db: float = 0.0
    triggered: bool = False


@dataclass
class Moment:
    streamer: str
    t_start: float           # event start (wall clock, chat delay removed)
    t_end: float
    peak_t: float
    peak_ratio: float
    peak_z: float
    windows: list = field(default_factory=list)

    def signals(self) -> dict:
        cats: Counter = Counter()
        for w in self.windows:
            for k, v in w.cats.items():
                cats[k] += v
        return {
            "peak_ratio": round(self.peak_ratio, 2),
            "peak_z": round(self.peak_z, 2),
            "max_messages": max((w.messages for w in self.windows), default=0),
            "max_users": max((w.users for w in self.windows), default=0),
            "baseline": round(min((w.baseline for w in self.windows), default=0), 2),
            "max_hype_share": round(max((w.hype_share for w in self.windows), default=0), 3),
            "max_repetition": round(max((w.repetition for w in self.windows), default=0), 3),
            "max_audio_spike_db": round(max((w.audio_spike_db for w in self.windows), default=0), 1),
            "categories": dict(cats),
            "windows": len(self.windows),
        }


class Timeline:
    """Time-indexed float series (audio loudness, motion) with bounded history."""

    def __init__(self, max_age: float):
        self.t: deque[float] = deque()
        self.v: deque[float] = deque()
        self.max_age = max_age
        self._lock = threading.Lock()

    def extend(self, t0: float, step: float, values) -> None:
        with self._lock:
            for i, val in enumerate(values):
                t = t0 + i * step
                if self.t and t <= self.t[-1]:
                    continue
                self.t.append(t)
                self.v.append(float(val))
            while self.t and self.t[-1] - self.t[0] > self.max_age:
                self.t.popleft()
                self.v.popleft()

    def slice(self, a: float, b: float) -> np.ndarray:
        with self._lock:
            ts = list(self.t)
            i, j = bisect.bisect_left(ts, a), bisect.bisect_right(ts, b)
            return np.array(list(self.v)[i:j], dtype=np.float32)

    def series(self, a: float, b: float) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            ts = list(self.t)
            i, j = bisect.bisect_left(ts, a), bisect.bisect_right(ts, b)
            return np.array(ts[i:j]), np.array(list(self.v)[i:j], dtype=np.float32)

    def median(self) -> float:
        with self._lock:
            return float(np.median(self.v)) if self.v else -60.0


class StreamDetector:
    def __init__(self, streamer: str, cfg_get: Callable[[str], object],
                 on_moment: Callable[[Moment], None],
                 on_bin: Callable[[int, int, int, int, float | None, float | None], None] | None = None):
        self.streamer = streamer
        self.get = cfg_get
        self.on_moment = on_moment
        self.on_bin = on_bin
        self.msgs: deque[ChatMsg] = deque()
        self.bins: deque[tuple[int, int]] = deque()   # (bin_start, count) history for baseline
        self._last_bin = None
        self.audio = Timeline(max_age=20 * 60)
        self.motion = Timeline(max_age=20 * 60)
        self.active: Moment | None = None
        self.last_final: Moment | None = None
        self.last_window: Window | None = None
        self._lock = threading.Lock()

    # ---- ingest --------------------------------------------------------
    def add_message(self, ts: float, user: str, text: str) -> None:
        with self._lock:
            self.msgs.append(ChatMsg(ts, user, text, frozenset(signals.classify(text))))

    def seed_bins(self, bins: list[tuple[int, int]]) -> None:
        """Restore baseline history after a restart."""
        with self._lock:
            for b in bins:
                self.bins.append(b)
            if bins:
                self._last_bin = bins[-1][0]

    def _recent(self, since: float) -> list[ChatMsg]:
        out = []
        for m in reversed(self.msgs):
            if m.ts < since - 5:  # small slack: messages can arrive slightly out of order
                break
            out.append(m)
        out.reverse()
        return out

    def messages_between(self, a: float, b: float) -> list[ChatMsg]:
        with self._lock:
            return [m for m in self.msgs if a <= m.ts <= b]

    # ---- baseline ------------------------------------------------------
    def _roll_bins(self, now: float) -> None:
        win = int(self.get("chat.window_sec"))
        cur = int(now // win * win)
        start = self._last_bin + win if self._last_bin is not None else cur - win
        if self._last_bin is None and self.msgs:
            start = int(self.msgs[0].ts // win * win)
        for b in range(start, cur, win):
            n = 0
            users, hype = set(), 0
            for m in self._recent(b):
                if b <= m.ts < b + win:
                    n += 1
                    users.add(m.user)
                    hype += bool(m.cats)
            base, mad = self._baseline(exclude_after=b)
            ratio = n / max(base, 1.0) if base is not None else None
            z = (n - base) / mad if base is not None else None
            self.bins.append((b, n))
            if self.on_bin:
                self.on_bin(b, n, len(users), hype, ratio, z)
        if cur - win >= start:
            self._last_bin = cur - win
        horizon = now - float(self.get("chat.baseline_minutes")) * 60
        while self.bins and self.bins[0][0] < horizon:
            self.bins.popleft()
        # keep 10 minutes of messages: clips are cut up to a few minutes after the moment
        while self.msgs and self.msgs[0].ts < now - 600:
            self.msgs.popleft()

    def _baseline(self, exclude_after: float) -> tuple[float | None, float]:
        # Leave out the last 60 s so the spike itself doesn't raise the baseline.
        hist = [c for (b, c) in self.bins if b < exclude_after - 60]
        if len(hist) < int(self.get("chat.min_baseline_windows")):
            return None, 1.0
        med = statistics.median(hist)
        mad = statistics.median(abs(c - med) for c in hist) * 1.4826
        # Poisson-like floor so quiet chats don't produce huge z-scores.
        return med, max(mad, (med ** 0.5), 1.0)

    # ---- step ----------------------------------------------------------
    def step(self, now: float) -> Window | None:
        """Evaluate the rolling window that ends at `now`. Returns the window analysed."""
        with self._lock:
            self._roll_bins(now)
            win = float(self.get("chat.window_sec"))
            t0, t1 = now - win, now
            ms = [m for m in self._recent(t0) if t0 <= m.ts < t1]
            base, mad = self._baseline(exclude_after=t0)
            n = len(ms)
            cats = Counter()
            for m in ms:
                cats.update(m.cats)
            hype_share = sum(1 for m in ms if m.cats) / n if n else 0.0
            w = Window(t0, t1, n, len({m.user for m in ms}), hype_share, dict(cats),
                       signals.repetition_share([m.text for m in ms]),
                       base if base is not None else 0.0,
                       n / max(base, 1.0) if base is not None else 0.0,
                       (n - base) / mad if base is not None else 0.0)
            w.audio_spike_db = self._audio_spike(t0 - float(self.get("chat.reaction_delay_sec")), t1)
            if base is not None:
                w.triggered = self._is_trigger(w)
            self.last_window = w
            self._track(w, now)
            return w

    def _audio_spike(self, a: float, b: float) -> float:
        seg = self.audio.slice(a, b)
        if len(seg) == 0:
            return 0.0
        return float(np.percentile(seg, 95) - self.audio.median())

    def _is_trigger(self, w: Window) -> bool:
        d = self.get
        if w.messages < int(d("detection.min_messages")):
            return False
        z_thr = float(d("detection.z_threshold"))
        r_thr = float(d("detection.ratio_threshold"))
        hype_thr = float(d("detection.hype_share_threshold"))
        # 1) clear chat explosion vs. the streamer's normal activity
        if w.z >= z_thr and w.ratio >= r_thr:
            return True
        # 2) smaller spike, but the messages are reactions (LOL, WTF, W, emote walls)
        reactive = w.hype_share >= hype_thr or w.repetition >= 0.25
        if reactive and w.z >= z_thr * 0.66 and w.ratio >= max(1.5, r_thr * 0.7):
            return True
        # 3) audio supports a near-threshold chat spike (audio never triggers alone)
        if w.audio_spike_db >= float(d("detection.audio_boost_db")) and w.z >= z_thr * 0.8 \
                and w.ratio >= r_thr * 0.8:
            return True
        return False

    def _track(self, w: Window, now: float) -> None:
        delay = float(self.get("chat.reaction_delay_sec"))
        merge_gap = float(self.get("detection.merge_gap_sec"))
        max_len = float(self.get("detection.max_moment_sec"))
        if w.triggered:
            ev0, ev1 = w.t0 - delay, w.t1 - delay
            a = self.active
            if a and ev0 <= a.t_end + merge_gap and ev1 - a.t_start <= max_len:
                a.t_end = max(a.t_end, ev1)
                a.windows.append(w)
                if w.ratio > a.peak_ratio:
                    a.peak_ratio, a.peak_z, a.peak_t = w.ratio, w.z, (w.t0 + w.t1) / 2 - delay
            else:
                if a:
                    self._finalize()
                lf = self.last_final
                cooldown = float(self.get("detection.cooldown_sec"))
                if lf and ev0 < lf.t_end + cooldown and w.ratio < lf.peak_ratio * 1.5:
                    return  # same moment still echoing in chat
                self.active = Moment(self.streamer, ev0, ev1, (w.t0 + w.t1) / 2 - delay, w.ratio, w.z, [w])
        elif self.active and (now - delay) - self.active.t_end > merge_gap:
            self._finalize()

    def _finalize(self) -> None:
        m, self.active = self.active, None
        if m:
            self.last_final = m
            self.on_moment(m)

    def flush(self) -> None:
        with self._lock:
            if self.active:
                self._finalize()
