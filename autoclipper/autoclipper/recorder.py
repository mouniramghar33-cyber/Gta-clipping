"""Live detection and continuous segmented recording (Streamlink -> FFmpeg segment muxer).

The stream is written as short MPEG-TS segments. Every segment gets a wall-clock start
and end time so it can be lined up with chat messages. Clips are cut from these
segments while the stream is still running.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

STREAMLINK = shutil.which("streamlink") or "streamlink"


@dataclass
class Segment:
    path: str
    start: float  # wall clock (unix seconds)
    end: float
    run: int

    @property
    def dur(self) -> float:
        return self.end - self.start


def stream_url(login: str, platform: str = "twitch") -> str:
    if platform == "twitch":
        return f"https://www.twitch.tv/{login}"
    if platform == "kick":
        return f"https://kick.com/{login}"
    if platform == "youtube":
        return f"https://www.youtube.com/@{login}/live"
    raise ValueError(f"unsupported platform {platform}")


def check_live(login: str, platform: str = "twitch", timeout: int = 45) -> dict | None:
    """Return stream metadata if live, else None. Uses Streamlink (no API keys needed)."""
    try:
        p = subprocess.run(
            [STREAMLINK, "--json", stream_url(login, platform)],
            capture_output=True, text=True, timeout=timeout,
        )
        data = json.loads(p.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError) as e:
        log.warning("live check failed for %s: %s", login, e)
        raise
    if "streams" not in data or not data["streams"]:
        return None
    md = data.get("metadata") or {}
    return {"title": md.get("title"), "category": md.get("category"), "author": md.get("author"),
            "qualities": list(data["streams"].keys())}


class SegmentStore:
    """Index of recorded segments for one streamer (persisted as JSON lines per session)."""

    def __init__(self):
        self._segs: list[Segment] = []
        self._lock = threading.Lock()

    def add(self, seg: Segment) -> None:
        with self._lock:
            self._segs.append(seg)

    def load_index(self, index_file: Path) -> None:
        if not index_file.exists():
            return
        for line in index_file.read_text().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if os.path.exists(d["path"]):
                self.add(Segment(**d))

    @property
    def latest_end(self) -> float:
        with self._lock:
            return self._segs[-1].end if self._segs else 0.0

    @property
    def earliest_start(self) -> float:
        with self._lock:
            return self._segs[0].start if self._segs else 0.0

    def range(self, t0: float, t1: float) -> list[Segment]:
        with self._lock:
            return [s for s in self._segs if s.end > t0 and s.start < t1]

    def all(self) -> list[Segment]:
        with self._lock:
            return list(self._segs)

    def prune(self, before: float, keep_ranges: list[tuple[float, float]]) -> int:
        """Delete segment files that end before `before` and are not inside a pinned range."""
        removed = 0
        with self._lock:
            keep: list[Segment] = []
            for s in self._segs:
                pinned = any(s.end > a and s.start < b for a, b in keep_ranges)
                if s.end < before and not pinned:
                    try:
                        os.remove(s.path)
                    except FileNotFoundError:
                        pass
                    removed += 1
                else:
                    keep.append(s)
            self._segs = keep
        return removed


class Recorder:
    """Records one live stream into segments; restarts itself if the pipeline dies."""

    def __init__(self, login: str, platform: str, session_dir: Path, store: SegmentStore,
                 quality: str, segment_sec: int = 2, disable_ads: bool = True,
                 on_segment: Callable[[Segment], None] | None = None):
        self.login, self.platform = login, platform
        self.dir = Path(session_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.quality = quality
        self.segment_sec = segment_sec
        self.disable_ads = disable_ads
        self.on_segment = on_segment
        self.index_file = self.dir / "index.jsonl"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._procs: list[subprocess.Popen] = []
        self.run = 0
        self.restarts = 0
        self.last_segment_at = 0.0
        self.running = False

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"rec-{self.login}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=15)
        self.running = False

    def _kill(self) -> None:
        for p in self._procs:
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGINT if "ffmpeg" in str(p.args[0]) else signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.time() + 8
        for p in self._procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
        self._procs = []

    def _loop(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            self.run += 1
            started = time.time()
            try:
                self._record_once()
            except Exception:  # noqa: BLE001 - keep the recorder alive no matter what
                log.exception("recorder %s crashed", self.login)
            self.running = False
            if self._stop.is_set():
                break
            # Recover quickly if it ran fine for a while, back off on repeated failures.
            backoff = 2 if time.time() - started > 120 else min(backoff * 2, 60)
            self.restarts += 1
            log.warning("recorder %s stopped, restarting in %ss", self.login, backoff)
            self._stop.wait(backoff)

    def _record_once(self) -> None:
        run = self.run
        csv_path = self.dir / f"list_r{run:03d}.csv"
        sl_cmd = [STREAMLINK, "--stdout", "--loglevel", "warning",
                  "--stream-segment-threads", "2", "--stream-timeout", "60", "--retry-open", "3"]
        if self.disable_ads and self.platform == "twitch":
            sl_cmd.append("--twitch-disable-ads")
        sl_cmd += [stream_url(self.login, self.platform), self.quality]
        ff_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts",
                  "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                  "-f", "segment", "-segment_time", str(self.segment_sec),
                  "-segment_format", "mpegts", "-segment_list", str(csv_path),
                  "-segment_list_type", "csv", str(self.dir / f"seg_r{run:03d}_%06d.ts")]
        log.info("recording %s (run %d) -> %s", self.login, run, self.dir)
        sl = subprocess.Popen(sl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ff = subprocess.Popen(ff_cmd, stdin=sl.stdout, stderr=subprocess.PIPE)
        sl.stdout.close()  # ffmpeg owns the pipe now
        self._procs = [sl, ff]
        self.running = True
        seen = 0
        while not self._stop.is_set():
            seen = self._poll_csv(csv_path, run, seen)
            if ff.poll() is not None or sl.poll() is not None:
                time.sleep(1)
                self._poll_csv(csv_path, run, seen)
                err = (sl.stderr.read() or b"").decode(errors="replace")[-500:] if sl.poll() is not None else ""
                ferr = (ff.stderr.read() or b"").decode(errors="replace")[-500:] if ff.poll() is not None else ""
                log.warning("pipeline for %s exited (streamlink=%s ffmpeg=%s) %s %s",
                            self.login, sl.poll(), ff.poll(), err.strip(), ferr.strip())
                break
            time.sleep(0.5)
        self._kill()

    def _poll_csv(self, csv_path: Path, run: int, seen: int) -> int:
        if not csv_path.exists():
            return seen
        lines = csv_path.read_text().splitlines()
        for line in lines[seen:]:
            parts = line.rsplit(",", 2)
            if len(parts) != 3:
                continue
            name, s, e = parts
            path = self.dir / name
            try:
                dur = float(e) - float(s)
                wall_end = path.stat().st_mtime
            except (ValueError, FileNotFoundError):
                continue
            seg = Segment(str(path), wall_end - dur, wall_end, run)
            self.store.add(seg)
            with self.index_file.open("a") as f:
                f.write(json.dumps(asdict(seg)) + "\n")
            self.last_segment_at = time.time()
            if self.on_segment:
                try:
                    self.on_segment(seg)
                except Exception:  # noqa: BLE001
                    log.exception("on_segment callback failed")
        return len(lines)
