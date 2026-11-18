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
from . import analytics, context, editor, metadata, render, scoring, signals, subtitles
from .layout import plan_layout
from .transcribe import Transcriber, Word

if TYPE_CHECKING:
    from .manager import Manager

log = logging.getLogger(__name__)
STEP = cutmod.STEP


class SkipClip(Exception):
    pass


def tier_for(score: float, tiers: dict) -> str:
    for name in ("immediate", "very_high", "high", "normal", "review"):
        if name in tiers and score >= tiers[name]:
            return name
    return "reject"


def _emotions(det, a: float, b: float) -> dict:
    """Strongest value of each facial emotion around the moment (from the real-time keyframes)."""
    names = ("happy", "surprise", "anger", "fear", "sad")
    out = {k: 0.0 for k in names}
    for v in det.emotions.slice(a, b):
        idx = int(v)
        if 0 <= idx < len(names):
            out[names[idx]] = max(out[names[idx]], float(v - idx))
    return out


def loop_end(src: Path, start: float, end: float, words, peak: float) -> float | None:
    """End on the frame (in the last 1.2 s) that looks most like the opening frame, so the clip
    loops naturally - only when it doesn't cut a word and the match is clearly good."""
    import subprocess
    def frames(ss, dur, fps):
        raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{ss:.3f}", "-i", str(src),
                              "-t", f"{dur:.3f}", "-vf", f"fps={fps},scale=64:36,format=gray", "-f", "rawvideo",
                              "pipe:1"], capture_output=True, timeout=60).stdout
        n = len(raw) // (64 * 36)
        return [np.frombuffer(raw[i * 2304:(i + 1) * 2304], np.uint8).astype(np.float32) for i in range(n)]
    try:
        first = frames(start, 0.2, 10)
        tail = frames(max(start, end - 1.2), 1.2, 10)
    except Exception:  # noqa: BLE001
        return None
    if not first or len(tail) < 3 or end - 1.2 < peak + 2:
        return None
    base = float(np.mean([np.mean(np.abs(f - first[0])) for f in tail]))
    best_i, best_d = None, 1e9
    for i, f in enumerate(tail):
        t = max(start, end - 1.2) + i / 10
        if any(w.start < t < w.end for w in words):
            continue
        d = float(np.mean(np.abs(f - first[0])))
        if d < best_d:
            best_i, best_d = i, d
    if best_i is None or best_d > 0.6 * base or best_d > 12:
        return None
    return round(max(start, end - 1.2) + best_i / 10, 2)


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

    def _trend(self, m: dict, ctx, category: str | None) -> tuple[float, str]:
        g = self.cfg["gta6"]
        if ctx.gta and m.get("gta"):
            bonus = 5 if ctx.gta_tags else 0
            return min(100.0, float(g.get("trend_score", 90)) + bonus), \
                "GTA VI release period" + (f" ({', '.join(ctx.gta_tags[:3])})" if ctx.gta_tags else "")
        if ctx.gta:
            return 70.0, "GTA VI content"
        perf = analytics.category_performance(self.db, self.cfg, category) if category else None
        if perf is not None:
            return max(20.0, min(90.0, 50 * perf)), f"category '{category}' performs {perf:.2f}x your median"
        return 50.0, ""

    def _popularity(self, st, streamer: str) -> tuple[float, str]:
        import math
        viewers = st.meta.get("viewers")
        row = self.db.one("SELECT followers FROM streamers WHERE login=?", (streamer,)) or {}
        bonus = analytics.streamer_bonus(self.db, self.cfg, streamer) if self.cfg.get("analytics.learning") else 0
        if viewers:
            s = max(10.0, min(100.0, 20 * math.log10(max(viewers, 1)) - 10))
            why = f"{viewers:,} live viewers"
        elif row.get("followers"):
            s = max(10.0, min(100.0, 15 * math.log10(row["followers"]) - 5))
            why = f"{row['followers']:,} followers"
        else:
            return 50.0 + bonus * 2, ""  # unknown audience size: neutral (not guessed)
        if bonus:
            why += f", your clips of them perform {bonus:+.0f}"
        return max(0.0, min(100.0, s + bonus * 2)), why

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
        # collaborations: the same moment is often on two watched streams - keep only one clip
        if m.get("collab"):
            partners = [p for p in m["collab"].split(",") if p]
            if partners:
                q = ",".join("?" * len(partners))
                other = db.one(f"SELECT id, streamer FROM clips WHERE streamer IN ({q}) AND src_start<=? AND src_end>=?",
                               (*partners, m["peak_t"] + 10, m["peak_t"] - 10))
                if other:
                    raise SkipClip(f"collab moment already clipped from {other['streamer']} (clip {other['id']})")

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
        tr = self.transcriber.transcribe(str(audio_wav), w.get("language"), w.get("beam_size", 1),
                                         w.get("initial_prompt"))

        # --- cut --------------------------------------------------------------
        plan = cutmod.plan_cut(dur, ev0, ev1, peak_hint, chat, audio_db, motion_s, tr.words, tr.segments,
                               ccfg["min_sec"], ccfg["max_sec"], ccfg["remove_silence"],
                               ccfg["min_silence_sec"], ccfg["keep_silence_sec"],
                               scene_cuts=cutmod.scene_cuts(motion, signals.VIDEO_FPS),
                               ideal=(ccfg.get("ideal_min_sec", 20), ccfg.get("ideal_max_sec", 40)),
                               preferred_max=ccfg.get("preferred_max_sec", 45), hard_max=min(60, ccfg["max_sec"]))
        if ccfg.get("loop_ending"):
            new_end = loop_end(src, plan.start, plan.keep[-1][1], tr.words, plan.peak)
            if new_end:
                plan.keep[-1] = (plan.keep[-1][0], new_end)
                plan.end = new_end
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

        # --- context understanding ------------------------------------------------
        sess0 = db.one("SELECT * FROM sessions WHERE id=?", (m["session_id"],)) if m["session_id"] else None
        category = (sess0 or {}).get("category") or m.get("category")
        emo = _emotions(st.detector, m["t_start"] - 5, m["t_end"] + 5)
        ctx = context.classify(streamer=streamer, transcript=" ".join(x.text for x in clip_words),
                               chat_texts=[x.text for x in clip_msgs], chat_cats=cats, chat_msgs=len(clip_msgs),
                               sig=sig, emotions=emo, category=category, title=(sess0 or {}).get("title"),
                               names=self.mgr.names, peak_ratio=float(m.get("peak_ratio") or 0))
        if cfg.get("metadata.llm_base_url") and cfg.get("metadata.llm_model"):
            op = context.llm_opinion(cfg.get("metadata.llm_base_url"), cfg.get("metadata.llm_model"),
                                     " ".join(x.text for x in clip_words),
                                     [x.text for x in clip_msgs], category, ctx.labels)
            if op:
                for lb in op.get("labels") or []:
                    if isinstance(lb, str):
                        ctx.labels[lb] = max(ctx.labels.get(lb, 0), 0.5)
                        ctx.evidence.setdefault(lb, []).append("local LLM")
                if op.get("entertaining") is False and max(ctx.labels.values(), default=0) < 0.5:
                    ctx.entertaining, ctx.reason = False, "LLM: " + str(op.get("summary", "not entertaining"))[:120]
        trend = self._trend(m, ctx, category)
        popularity = self._popularity(st, streamer)

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
            weights=cfg.get("scoring.weights"), context=ctx.as_dict(), trend=trend, popularity=popularity)
        tier = tier_for(score, cfg.get("scoring.tiers") or {})

        # --- metadata -----------------------------------------------------------
        sess = db.one("SELECT * FROM sessions WHERE id=?", (m["session_id"],)) if m["session_id"] else None
        src_start = render.media_to_wall(plan.start, mapping)
        src_end = render.media_to_wall(plan.end, mapping)
        offset = None
        if sess and sess.get("stream_started_at"):
            offset = m["peak_t"] - sess["stream_started_at"]
        md = metadata.build_metadata(
            streamer=streamer, display=st.display_name, category=category,
            collab=[self.mgr.states[c].display_name or c for c in ctx.collab if c in self.mgr.states],
            gta=ctx.gta, labels=ctx.as_dict(),
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
        if real_dur < float(ccfg.get("ideal_min_sec", 20)) and score < float(cfg.get("scoring.short_clip_min_score") or 85):
            status = "auto_rejected"   # short clips only when they're very strong
        if not ctx.entertaining and score < float(cfg.get("scoring.tiers.high") or 85):
            status = "auto_rejected"   # ordinary conversation
        if real_dur > float(ccfg["max_sec"]) + 0.5:
            raise RuntimeError(f"rendered clip is {real_dur:.1f}s, above the {ccfg['max_sec']}s maximum")
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
            "labels": ctx.as_dict(), "gta": int(ctx.gta), "collab": ",".join(ctx.collab) or None,
            "tier": tier, "platform": m.get("platform"), "latency": time.time() - m["peak_t"],
            "category": sess.get("category") if sess else None, "quote": md.get("quote"),
        })
        db.execute("INSERT OR REPLACE INTO clip_variants (clip_id, style, path, created_at) VALUES (?,?,?,?)",
                   (clip_id, editor.style_for(cfg, default_style)[0], str(out), time.time()))
        comps.pop("fingerprint", None)
        log.info("clip %s: %s score=%.1f tier=%s type=%s%s%s (%s, %.1fs) ready %.0fs after the moment",
                 clip_id, streamer, score, tier, ctx.primary, " GTA" if ctx.gta else "",
                 f" collab={ctx.collab}" if ctx.collab else "", layout.kind, real_dur, time.time() - m["peak_t"])
        self.mgr.after_clip(clip_id)
        return clip_id
