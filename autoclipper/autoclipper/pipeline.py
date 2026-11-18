"""Moment -> finished vertical clip (extract, transcribe, cut, layout, subtitles, render, score)."""
from __future__ import annotations

import logging
import secrets
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from . import cut as cutmod
from . import editor, metadata, render, scoring, signals, subtitles
from .layout import plan_layout
from .transcribe import Transcriber, Word

if TYPE_CHECKING:
    from .manager import Manager

log = logging.getLogger(__name__)
STEP = cutmod.STEP


class SkipClip(Exception):
    pass


def _resample(values: np.ndarray, src_step: float, dst_step: float, n: int) -> np.ndarray:
    if len(values) == 0:
        return np.zeros(n, dtype=np.float32)
    t_src = np.arange(len(values)) * src_step
    t_dst = np.arange(n) * dst_step
    return np.interp(t_dst, t_src, values).astype(np.float32)


def _select(arr: np.ndarray, keep: list[tuple[float, float]]) -> np.ndarray:
    parts = [arr[int(a / STEP): int(b / STEP)] for a, b in keep]
    return np.concatenate(parts) if parts else np.zeros(0)


class ClipPipeline:
    def __init__(self, mgr: "Manager"):
        self.mgr = mgr
        self.cfg = mgr.cfg
        self.db = mgr.db
        w = self.cfg["whisper"]
        self.transcriber = Transcriber(w["model"], w["device"], w["compute_type"], str(self.cfg.models_dir))

    def process(self, moment_id: int) -> int | None:
        db, cfg = self.db, self.cfg
        m = db.one("SELECT * FROM moments WHERE id=?", (moment_id,))
        if not m:
            return None
        db.update("moments", moment_id, {"status": "processing", "updated_at": time.time()})
        work = cfg.work_dir / f"m{moment_id}"
        work.mkdir(parents=True, exist_ok=True)
        t_begin = time.time()
        try:
            clip_id = self._process(m, work)
            db.update("moments", moment_id, {"status": "done", "updated_at": time.time(), "error": None})
            log.info("moment %s -> clip %s in %.0fs", moment_id, clip_id, time.time() - t_begin)
            return clip_id
        except SkipClip as e:
            db.update("moments", moment_id, {"status": "skipped", "error": str(e), "updated_at": time.time()})
            log.info("moment %s skipped: %s", moment_id, e)
        except Exception as e:  # noqa: BLE001
            log.exception("moment %s failed", moment_id)
            db.update("moments", moment_id, {"status": "failed", "error": str(e)[-500:], "updated_at": time.time()})
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return None

    def _process(self, m: dict, work: Path) -> int:
        cfg, db = self.cfg, self.db
        streamer = m["streamer"]
        st = self.mgr.state(streamer)
        ccfg = cfg["clip"]
        win0 = m["t_start"] - ccfg["pre_sec"]
        win1 = m["t_end"] + ccfg["post_sec"]

        # duplicate prevention: the event is already covered by an existing clip
        dup = db.one("SELECT id FROM clips WHERE streamer=? AND src_start<=? AND src_end>=?",
                     (streamer, m["peak_t"], m["peak_t"]))
        if dup:
            raise SkipClip(f"already covered by clip {dup['id']}")

        segs = st.store.range(win0, win1)
        if not segs:
            raise SkipClip("source segments no longer available")
        src = work / "src.ts"
        mapping = render.extract_window(segs, src)
        dur = render.probe_duration(src)
        if dur < ccfg["min_sec"]:
            raise SkipClip(f"only {dur:.0f}s of source video available")

        # --- signals in media time ------------------------------------------
        loud, motion = signals.segment_features(str(src))
        n = int(np.ceil(dur / STEP))
        audio_db = _resample(loud, signals.AUDIO_HOP, STEP, n)
        motion_s = _resample(motion, 1 / signals.VIDEO_FPS, STEP, n)
        delay = float(cfg.get("chat.reaction_delay_sec"))
        msgs = st.detector.messages_between(win0 - 5, win1 + delay + 5)
        chat = np.zeros(n, dtype=np.float32)
        for msg in msgs:
            i = int(render.wall_to_media(msg.ts - delay, mapping) / STEP)
            if 0 <= i < n:
                chat[i] += 1
        ev0 = render.wall_to_media(m["t_start"], mapping)
        ev1 = render.wall_to_media(m["t_end"], mapping)
        peak_hint = render.wall_to_media(m["peak_t"], mapping)

        # --- transcription --------------------------------------------------
        audio_wav = work / "audio.wav"
        render.extract_audio(src, audio_wav)
        w = cfg["whisper"]
        tr = self.transcriber.transcribe(str(audio_wav), w.get("language"), w.get("beam_size", 5))

        # --- cut --------------------------------------------------------------
        plan = cutmod.plan_cut(dur, ev0, ev1, peak_hint, chat, audio_db, motion_s, tr.words, tr.segments,
                               ccfg["min_sec"], ccfg["max_sec"], ccfg["remove_silence"],
                               ccfg["min_silence_sec"], ccfg["keep_silence_sec"],
                               scene_cuts=cutmod.scene_cuts(motion, signals.VIDEO_FPS))
        keep = plan.keep
        out_dur = plan.duration
        peak_out = cutmod.map_time(plan.peak, keep) or 0.0

        # --- layout + subtitles ------------------------------------------------
        rc, sc = cfg["render"], cfg["subtitles"]
        layout = plan_layout(str(src), plan.start, plan.end - plan.start, rc["width"], rc["height"])
        clip_words = [x for x in tr.words if plan.start - 0.05 <= x.start < plan.end]
        clip_msgs = [x for x in msgs if win0 <= x.ts - delay <= win1]
        chat_top = [t for t, _ in signals.top_tokens([x.text for x in clip_msgs], 8)]
        loud_thr = float(np.percentile(audio_db, 90)) if len(audio_db) else 0
        loud_times = [i * STEP for i, v in enumerate(audio_db) if v >= loud_thr]
        important = subtitles.important_words(tr.words, set(chat_top), loud_times)
        # keep highlight indices aligned with the full word list
        imp_clip = {clip_words.index(x) for i, x in enumerate(tr.words) if i in important and x in clip_words}

        cats: dict = {}
        for x in clip_msgs:
            for c in x.cats:
                cats[c] = cats.get(c, 0) + 1
        sig = m["signals"] or {}
        top_cat = None
        reaction = {k: v for k, v in cats.items() if k != "clip"}
        if reaction:
            top_cat = max(reaction, key=reaction.get)

        # --- score ------------------------------------------------------------
        speech = np.zeros(n, dtype=np.float32)
        for x in tr.words:
            speech[int(x.start / STEP): int(np.ceil(x.end / STEP))] = 1
        out_words = [Word(cutmod.map_time(x.start, keep) or 0, (cutmod.map_time(x.start, keep) or 0)
                          + (x.end - x.start), x.text, x.prob)
                     for x in clip_words if cutmod.map_time(x.start, keep) is not None]
        hist = db.query("SELECT scores FROM clips WHERE streamer=? AND created_at>? ORDER BY id DESC LIMIT 50",
                        (streamer, time.time() - 86400))
        hist_fp = [h["scores"].get("fingerprint", []) for h in hist if isinstance(h["scores"], dict)]
        ratios = [r["peak_ratio"] for r in db.query(
            "SELECT peak_ratio FROM moments WHERE streamer=? AND peak_ratio IS NOT NULL ORDER BY id DESC LIMIT 200",
            (streamer,))]
        score, comps, expl, fp = scoring.score_clip(
            sig=sig, words=out_words, out_duration=out_dur, interest_out=_select(plan.interest, keep),
            speech_out=_select(speech, keep), audio_out_db=_select(audio_db, keep), peak_out=peak_out,
            chat_cats=cats, chat_msgs=len(clip_msgs), transcript_text=" ".join(x.text for x in clip_words),
            chat_top=chat_top, history_fps=hist_fp, history_ratios=ratios,
            weights=cfg.get("scoring.weights"))

        # --- metadata -----------------------------------------------------------
        sess = db.one("SELECT * FROM sessions WHERE id=?", (m["session_id"],)) if m["session_id"] else None
        src_start = render.media_to_wall(plan.start, mapping)
        src_end = render.media_to_wall(plan.end, mapping)
        offset = None
        if sess and sess.get("stream_started_at"):
            offset = m["peak_t"] - sess["stream_started_at"]
        md = metadata.build_metadata(
            streamer=streamer, display=st.display_name, category=sess.get("category") if sess else None,
            top_cat=top_cat, words=out_words, peak_out=peak_out, score=score, explanation=expl,
            src_start=src_start, src_end=src_end, peak_wall=m["peak_t"], stream_offset=offset,
            default_tags=cfg.get("metadata.default_hashtags"),
            credit_template=cfg.get("publishing.credit_template"), layout_kind=layout.kind,
            llm_base_url=cfg.get("metadata.llm_base_url"), llm_model=cfg.get("metadata.llm_model"))

        # --- master + default render ------------------------------------------
        day = time.strftime("%Y-%m-%d", time.gmtime())
        out_dir = cfg.clips_dir / day
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{streamer}_{int(m['peak_t'])}_m{m['id']}"
        master_dir = cfg.clips_dir / "masters" / f"m{m['id']}"
        editor.save_master(master_dir, src, {
            "keep": keep, "start": plan.start, "end": plan.end, "peak_out": peak_out,
            "words": [asdict(x) for x in clip_words], "important": sorted(imp_clip),
            "layout": editor.layout_to_dict(layout), "hook": md["hook"], "top_cat": top_cat,
            "score": score, "streamer": streamer,
        })
        default_style = cfg.get("render.default_style") or "clean"
        out = out_dir / f"{stem}.mp4"
        editor.render_style(cfg, master_dir, default_style, out, work)
        thumb = out_dir / f"{stem}.jpg"
        render.make_thumbnail(out, peak_out, thumb)
        real_dur = render.probe_duration(out)

        status = "pending" if score >= cfg.get("scoring.min_score_to_keep") else "auto_rejected"
        comps["fingerprint"] = fp
        clip_id = db.insert("clips", {
            "moment_id": m["id"], "streamer": streamer, "path": str(out), "thumb": str(thumb),
            "duration": real_dur, "score": score, "scores": comps,
            "priority": int(score >= cfg.get("scoring.priority_threshold")),
            "title": md["title"], "hook": md["hook"], "description": md["description"],
            "hashtags": md["hashtags"], "thumbnail_suggestion": md["thumbnail_suggestion"],
            "explanation": md["explanation"] + " | timestamps: " + str(md["timestamps"]),
            "transcript": " ".join(x.text for x in clip_words), "language": tr.language,
            "layout": layout.kind, "src_start": src_start, "src_end": src_end, "peak_t": m["peak_t"],
            "stream_offset": offset, "status": status, "public_token": secrets.token_urlsafe(24),
            "created_at": time.time(), "master_dir": str(master_dir), "top_cat": top_cat,
            "category": sess.get("category") if sess else None, "quote": md.get("quote"),
        })
        db.execute("INSERT OR REPLACE INTO clip_variants (clip_id, style, path, created_at) VALUES (?,?,?,?)",
                   (clip_id, editor.style_for(cfg, default_style)[0], str(out), time.time()))
        log.info("clip %s: %s score=%.1f %s (%s, %.1fs)", clip_id, streamer, score, comps, layout.kind, real_dur)
        self.mgr.after_clip(clip_id)
        return clip_id
