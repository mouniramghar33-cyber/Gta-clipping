"""Orchestrator: live checks, recording, chat, detection, clip jobs, cleanup, publishing."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from . import twitch_api
from .chat import TwitchChat
from .config import Config
from .db import DB
from .detector import Moment, StreamDetector
from .notify import Notifier
from .publishing import Engine
from .secure import Vault
from .recorder import Recorder, Segment, SegmentStore, check_live
from .signals import AUDIO_HOP, VIDEO_FPS, segment_features

log = logging.getLogger(__name__)


@dataclass
class StreamerState:
    login: str
    platform: str
    detector: StreamDetector
    store: SegmentStore = field(default_factory=SegmentStore)
    display_name: str | None = None
    recorder: Recorder | None = None
    live: bool = False
    meta: dict = field(default_factory=dict)
    session_id: int | None = None
    offline_checks: int = 0
    last_check: float = 0.0
    last_error: str | None = None


class Manager:
    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        for d in (cfg.segments_dir, cfg.clips_dir, cfg.work_dir, cfg.models_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.states: dict[str, StreamerState] = {}
        self.chat = TwitchChat(self._on_chat)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.render_pool = ThreadPoolExecutor(max_workers=int(cfg.get("render.workers")), thread_name_prefix="render")
        self.feature_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="features")
        self.publish_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="publish")
        self.inflight: set[int] = set()
        self.tasks: list[asyncio.Task] = []
        self.vault = Vault(cfg.data_dir, db)
        self.notifier = Notifier(cfg, db, self.vault)
        self.engine = Engine(cfg, db, self.vault, self.notifier)
        twitch_api.configure(self.vault.get)
        self.started_at = time.time()
        psutil.cpu_percent(interval=None)  # prime the counter so the first reading isn't 0
        self.data_size = 0
        from .pipeline import ClipPipeline
        self.pipeline = ClipPipeline(self)

    # ---- state -----------------------------------------------------------
    def state(self, login: str) -> StreamerState:
        login = login.lower()
        if login not in self.states:
            row = self.db.one("SELECT * FROM streamers WHERE login=?", (login,)) or {}
            det = StreamDetector(login, self.cfg.get, self._on_moment_threadsafe,
                                 lambda *a, _l=login: self.db.upsert_chat_bin(_l, *a))
            self.states[login] = StreamerState(login, row.get("platform", "twitch"), det,
                                               display_name=row.get("display_name"))
            bins = self.db.query("SELECT t, messages FROM chat_bins WHERE streamer=? AND t>=? ORDER BY t",
                                 (login, time.time() - float(self.cfg.get("chat.baseline_minutes")) * 60))
            det.seed_bins([(b["t"], b["messages"]) for b in bins])
            # recover segments from earlier sessions (for queued moments after a restart)
            sdir = self.cfg.segments_dir / login
            if sdir.exists():
                for sess in sorted(sdir.iterdir()):
                    self.states[login].store.load_index(sess / "index.jsonl")
        return self.states[login]

    # ---- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        for s in self.cfg.get("streamers") or []:
            if isinstance(s, str):
                self.db.add_streamer(s)
            else:
                self.db.add_streamer(s["login"], s.get("platform", "twitch"), s.get("rights_granted", False))
        # crash recovery: half-finished work goes back into the queue
        self.db.execute("UPDATE moments SET status='queued' WHERE status IN ('processing','open')")
        self.db.execute("UPDATE sessions SET ended_at=? WHERE ended_at IS NULL", (time.time(),))
        for row in self.db.streamers():
            self.state(row["login"])
        self.tasks = [asyncio.create_task(c(), name=c.__name__) for c in
                      (self.chat.run_forever, self._live_loop, self._detect_loop, self._job_loop,
                       self._cleanup_loop)]
        log.info("manager started with %d streamers", len(self.states))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for st in self.states.values():
            if st.recorder:
                await asyncio.to_thread(st.recorder.stop)
            st.detector.flush()
        self.render_pool.shutdown(wait=False, cancel_futures=True)

    # ---- chat ------------------------------------------------------------
    def _on_chat(self, channel: str, ts: float, user: str, text: str) -> None:
        st = self.states.get(channel)
        if st and st.live:
            st.detector.add_message(ts, user, text)

    # ---- live detection --------------------------------------------------
    async def _live_loop(self) -> None:
        sem = asyncio.Semaphore(4)
        while True:
            rows = self.db.streamers(enabled_only=True)
            enabled = {r["login"] for r in rows}
            for login, st in list(self.states.items()):
                if login not in enabled and st.live:
                    await self._go_offline(st)
            helix: dict | None = None
            twitch_rows = [r["login"] for r in rows if r["platform"] == "twitch"]
            if twitch_api.enabled() and twitch_rows:
                try:
                    helix = await asyncio.to_thread(twitch_api.live_streams, twitch_rows)
                except Exception as e:  # noqa: BLE001
                    log.warning("helix check failed, falling back to streamlink: %s", e)

            async def check(r):
                st = self.state(r["login"])
                st.display_name = r.get("display_name") or st.display_name
                try:
                    if helix is not None and r["platform"] == "twitch":
                        meta = helix.get(r["login"])
                    else:
                        async with sem:
                            meta = await asyncio.to_thread(check_live, r["login"], r["platform"])
                    st.last_error = None
                except Exception as e:  # noqa: BLE001
                    st.last_error = f"live check: {e}"
                    return
                st.last_check = time.time()
                if meta:
                    st.offline_checks = 0
                    st.meta = meta
                    if meta.get("author") and not st.display_name:
                        st.display_name = meta["author"]
                        self.db.execute("UPDATE streamers SET display_name=? WHERE login=?", (meta["author"], st.login))
                    if not st.live:
                        await self._go_live(st)
                    elif st.session_id:
                        self.db.update("sessions", st.session_id, {"title": meta.get("title"),
                                                                   "category": meta.get("category")})
                elif st.live:
                    st.offline_checks += 1
                    if st.offline_checks >= 2:
                        await self._go_offline(st)

            await asyncio.gather(*(check(r) for r in rows))
            self._wake.clear()
            try:  # sleep until the next interval, or until a streamer is added
                await asyncio.wait_for(self._wake.wait(), float(self.cfg.get("live_check_interval_sec")))
            except asyncio.TimeoutError:
                pass

    async def _go_live(self, st: StreamerState) -> None:
        rc = self.cfg["recording"]
        sdir = self.cfg.segments_dir / st.login / time.strftime("%Y%m%d-%H%M%S")
        st.session_id = self.db.insert("sessions", {
            "streamer": st.login, "dir": str(sdir), "started_at": time.time(),
            "title": st.meta.get("title"), "category": st.meta.get("category"),
            "stream_started_at": st.meta.get("started_at")})
        st.recorder = Recorder(st.login, st.platform, sdir, st.store, rc["quality"], int(rc["segment_sec"]),
                               bool(rc["disable_ads"]), on_segment=lambda seg, _s=st: self._on_segment(_s, seg))
        st.recorder.start()
        st.live = True
        if st.platform == "twitch":
            await self.chat.join(st.login)
        log.info("%s is live: %s [%s]", st.login, st.meta.get("title"), st.meta.get("category"))

    async def _go_offline(self, st: StreamerState) -> None:
        log.info("%s went offline", st.login)
        st.live = False
        if st.recorder:
            await asyncio.to_thread(st.recorder.stop)
            st.recorder = None
        st.detector.flush()
        if st.session_id:
            self.db.update("sessions", st.session_id, {"ended_at": time.time()})
            st.session_id = None
        if st.platform == "twitch":
            await self.chat.part(st.login)

    def _on_segment(self, st: StreamerState, seg: Segment) -> None:
        def work():
            try:
                loud, motion = segment_features(seg.path)
                st.detector.audio.extend(seg.start, AUDIO_HOP, loud)
                st.detector.motion.extend(seg.start, 1 / VIDEO_FPS, motion)
            except Exception as e:  # noqa: BLE001
                log.debug("feature extraction failed for %s: %s", seg.path, e)
        self.feature_pool.submit(work)

    # ---- detection ---------------------------------------------------------
    async def _detect_loop(self) -> None:
        while True:
            now = time.time() - 1.0  # let late messages arrive
            for st in list(self.states.values()):
                if st.live:
                    try:
                        st.detector.step(now)
                    except Exception:  # noqa: BLE001
                        log.exception("detector step failed for %s", st.login)
            await asyncio.sleep(float(self.cfg.get("chat.step_sec")))

    def _on_moment_threadsafe(self, m: Moment) -> None:
        st = self.states.get(m.streamer)
        now = time.time()
        mid = self.db.insert("moments", {
            "streamer": m.streamer, "session_id": st.session_id if st else None,
            "t_start": m.t_start, "t_end": m.t_end, "peak_t": m.peak_t, "peak_ratio": m.peak_ratio,
            "peak_z": m.peak_z, "signals": m.signals(), "status": "queued",
            "created_at": now, "updated_at": now})
        log.info("moment %s detected for %s: ratio %.1fx z %.1f (%.0fs)", mid, m.streamer, m.peak_ratio,
                 m.peak_z, m.t_end - m.t_start)

    # ---- clip jobs -----------------------------------------------------------
    async def _job_loop(self) -> None:
        while True:
            try:
                post = float(self.cfg.get("clip.post_sec"))
                queued = self.db.query("SELECT * FROM moments WHERE status='queued' ORDER BY peak_ratio DESC")
                for m in queued:
                    if m["id"] in self.inflight:
                        continue
                    st = self.state(m["streamer"])
                    ready = st.store.latest_end >= m["t_end"] + post + 2
                    stale = time.time() - m["t_end"] > post + 600
                    ended = not st.live or (st.recorder and time.time() - st.recorder.last_segment_at > 60)
                    if ready or stale or (ended and time.time() > m["t_end"] + post):
                        self.inflight.add(m["id"])
                        fut = self.render_pool.submit(self.pipeline.process, m["id"])
                        fut.add_done_callback(lambda _f, _id=m["id"]: self.inflight.discard(_id))
            except Exception:  # noqa: BLE001
                log.exception("job loop error")
            await asyncio.sleep(3)

    # ---- storage ---------------------------------------------------------------
    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.cleanup)
            except Exception:  # noqa: BLE001
                log.exception("cleanup failed")
            await asyncio.sleep(float(self.cfg.get("storage.cleanup_interval_min")) * 60)

    def cleanup(self) -> dict:
        cfg, db = self.cfg, self.db
        now = time.time()
        pre, post = float(cfg.get("clip.pre_sec")), float(cfg.get("clip.post_sec"))
        removed_segments = 0
        for st in self.states.values():
            pins = [(m["t_start"] - pre - 10, m["t_end"] + post + 10) for m in db.query(
                "SELECT t_start, t_end FROM moments WHERE streamer=? AND status IN ('queued','processing')",
                (st.login,))]
            # moments of a live stream that may still be detected need the recent buffer
            before = now - float(cfg.get("recording.buffer_minutes")) * 60
            removed_segments += st.store.prune(before, pins)
        # empty / finished session directories
        for sdir in cfg.segments_dir.glob("*/*"):
            if sdir.is_dir() and not any(sdir.glob("*.ts")):
                active = any(st.recorder and st.recorder.dir == sdir for st in self.states.values())
                if not active:
                    shutil.rmtree(sdir, ignore_errors=True)
        removed_clips = 0
        rej_days = float(cfg.get("storage.rejected_retention_days"))
        keep_days = float(cfg.get("storage.clip_retention_days"))
        for c in db.query("SELECT * FROM clips WHERE status IN ('rejected','auto_rejected') AND created_at<?",
                          (now - rej_days * 86400,)):
            removed_clips += self._delete_clip_files(c)
        for c in db.query("SELECT * FROM clips WHERE status IN ('pending','published') AND created_at<?",
                          (now - keep_days * 86400,)):
            removed_clips += self._delete_clip_files(c)
        # disk cap: drop the weakest undecided clips first
        cap = float(cfg.get("storage.max_storage_gb")) * 1024 ** 3
        self.data_size = _dir_size(cfg.data_dir)
        if self.data_size > cap:
            for c in db.query("SELECT * FROM clips WHERE status IN ('auto_rejected','rejected','pending') "
                              "ORDER BY status='pending', score ASC, created_at ASC"):
                if self.data_size <= cap * 0.9:
                    break
                size = sum(os.path.getsize(p) for p in (c["path"], c["thumb"]) if p and os.path.exists(p))
                removed_clips += self._delete_clip_files(c)
                self.data_size -= size
        # clip masters (source + edit decisions) are only needed while a clip may still be
        # (re)rendered for an account: keep them while jobs are open, at most master_retention_days
        mdays = float(cfg.get("storage.master_retention_days") or 3)
        for c in db.query("SELECT id, master_dir, created_at FROM clips WHERE master_dir IS NOT NULL AND master_dir!=''"):
            open_jobs = db.one(f"SELECT COUNT(*) n FROM publish_jobs WHERE clip_id=? AND status IN "
                               f"('awaiting_approval','scheduled','uploading')", (c["id"],))["n"]
            if (now - c["created_at"] > mdays * 86400 and not open_jobs) or \
                    db.one("SELECT status FROM clips WHERE id=?", (c["id"],))["status"] in ("rejected", "deleted"):
                shutil.rmtree(c["master_dir"], ignore_errors=True)
                db.execute("UPDATE clips SET master_dir='' WHERE id=?", (c["id"],))
        db.execute("DELETE FROM chat_bins WHERE t<?", (now - 3 * 86400,))
        for d in cfg.work_dir.glob("m*"):
            if d.is_dir() and now - d.stat().st_mtime > 3600 and int(d.name[1:] or 0) not in self.inflight:
                shutil.rmtree(d, ignore_errors=True)
        self.data_size = _dir_size(cfg.data_dir)
        if removed_segments or removed_clips:
            log.info("cleanup: %d segments, %d clips removed", removed_segments, removed_clips)
        return {"segments_removed": removed_segments, "clips_removed": removed_clips,
                "data_bytes": self.data_size}

    def _delete_clip_files(self, c: dict) -> int:
        if self.db.one("SELECT COUNT(*) n FROM publish_jobs WHERE clip_id=? AND status IN "
                       "('awaiting_approval','scheduled','uploading','processing')", (c["id"],))["n"]:
            return 0  # still needed for publishing
        variants = [v["path"] for v in self.db.query("SELECT path FROM clip_variants WHERE clip_id=?", (c["id"],))]
        for p in (c["path"], c["thumb"], *variants):
            if p and os.path.exists(p):
                os.remove(p)
        if c.get("master_dir"):
            shutil.rmtree(c["master_dir"], ignore_errors=True)
        self.db.execute("DELETE FROM clip_variants WHERE clip_id=?", (c["id"],))
        self.db.execute("UPDATE clips SET status=CASE WHEN status='published' THEN 'published' ELSE 'deleted' END, "
                        "path='' WHERE id=?", (c["id"],))
        return 1

    # ---- streamers -------------------------------------------------------------
    def add_streamer(self, login: str, platform: str = "twitch", rights: bool = False) -> None:
        login = login.strip().lower().lstrip("@")
        if not login.replace("_", "").isalnum() or len(login) > 25:
            raise ValueError("invalid channel name")
        if platform not in ("twitch", "kick", "youtube"):
            raise ValueError("platform must be twitch, kick or youtube")
        self.db.add_streamer(login, platform, rights)
        self.state(login)
        if self.loop:
            self.loop.call_soon_threadsafe(self._wake.set)

    async def remove_streamer(self, login: str) -> None:
        st = self.states.pop(login, None)
        if st and st.live:
            await self._go_offline(st)
        self.db.execute("DELETE FROM streamers WHERE login=?", (login,))

    # ---- publishing (the worker process uploads; here we only plan) ----------------
    def after_clip(self, clip_id: int) -> None:
        c = self.db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not c:
            return
        if c["score"] >= float(self.cfg.get("notifications.viral_score")):
            self.notifier.send("viral_clip", f"🔥 Viral clip ({c['score']:.0f}/100) from {c['streamer']}: {c['title']}")
        try:
            self.engine.plan_clip(clip_id)
        except Exception:  # noqa: BLE001 - planning problems must never break clipping
            log.exception("planning clip %s failed", clip_id)

    # ---- status ------------------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        streamers = []
        rows = {r["login"]: r for r in self.db.streamers()}
        for login, row in rows.items():
            st = self.state(login)
            w = st.detector.last_window
            rec = st.recorder
            streamers.append({
                "login": login, "platform": row["platform"], "display_name": st.display_name,
                "enabled": bool(row["enabled"]), "rights_granted": bool(row["rights_granted"]),
                "live": st.live, "recording": bool(rec and rec.running),
                "segment_age": round(now - rec.last_segment_at, 1) if rec and rec.last_segment_at else None,
                "recorder_restarts": rec.restarts if rec else 0,
                "title": st.meta.get("title") if st.live else None,
                "category": st.meta.get("category") if st.live else None,
                "viewers": st.meta.get("viewers") if st.live else None,
                "last_check": st.last_check, "error": st.last_error,
                "chat": None if not (w and st.live) else {
                    "messages": w.messages, "ratio": round(w.ratio, 2), "z": round(w.z, 2),
                    "baseline": round(w.baseline, 1), "hype": round(w.hype_share, 2),
                    "audio_db": round(w.audio_spike_db, 1), "triggered": w.triggered},
                "buffer_sec": round(st.store.latest_end - st.store.earliest_start) if st.store.all() else 0,
            })
        du = shutil.disk_usage(self.cfg.data_dir)
        vm = psutil.virtual_memory()
        return {
            "streamers": streamers,
            "chat_connected": self.chat.connected,
            "queue": {"queued": self.db.one("SELECT COUNT(*) n FROM moments WHERE status='queued'")["n"],
                      "processing": len(self.inflight)},
            "server": {"cpu": psutil.cpu_percent(interval=None), "cpus": psutil.cpu_count(),
                       "load": os.getloadavg(), "mem_used": vm.used, "mem_total": vm.total,
                       "disk_used": du.used, "disk_total": du.total, "data_bytes": self.data_size,
                       "uptime": now - self.started_at},
            "publishers": {k: p.configured() for k, p in self.engine.providers.items()},
            "worker": self.db.kv_get("worker_heartbeat"),
            "publishing": {"dry_run": self.cfg.get("publishing.dry_run"),
                           "emergency_stop": self.cfg.get("publishing.emergency_stop"),
                           "awaiting_approval": self.db.one("SELECT COUNT(*) n FROM publish_jobs WHERE "
                                                            "status='awaiting_approval'")["n"],
                           "scheduled": self.db.one("SELECT COUNT(*) n FROM publish_jobs WHERE "
                                                    "status='scheduled'")["n"]},
        }


def _dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
