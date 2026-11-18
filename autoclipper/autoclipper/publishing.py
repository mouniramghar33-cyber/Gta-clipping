"""Publishing engine: persistent queue of (clip, account) jobs.

For every finished clip and every connected account:
  quality check -> rights/permissions -> score (with learned streamer bonus) -> platform
  metadata -> duplicate check -> best time slot (daily limit, min interval, posting times)
  -> approval when the platform requires it -> upload -> verify -> store URL/status.

Job states:
  awaiting_approval -> scheduled -> uploading -> processing -> published
                                          \\-> scheduled (retry with backoff) -> failed
  dry_run (dry-run mode: everything except the upload), cancelled, skipped
All state lives in SQLite, so a crash or restart never loses or duplicates an upload: jobs
caught mid-upload resume from their saved upload state (YouTube resumable session, TikTok
publish_id, Buffer post id, Upload-Post request id).
"""
from __future__ import annotations

import logging
import math
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import analytics, editor
from .scoring import jaccard
from .social import AuthError, Context, Permanent, ProviderError, Transient
from .social.buffer import Buffer
from .social.instagram import Instagram
from .social.tiktok import TikTok, validate_approval
from .social.uploadpost import UploadPost
from .social.youtube import YouTube

log = logging.getLogger(__name__)

ACTIVE = ("awaiting_approval", "scheduled", "uploading", "processing")
COUNTED = ("scheduled", "uploading", "processing", "published", "awaiting_approval")
MAX_LEN = {"youtube": 60, "tiktok": 60, "instagram": 60}   # your rule: never publish anything over 60 s


def providers(ctx: Context) -> dict:
    return {p.name: p for p in (YouTube(ctx), TikTok(ctx), Buffer(ctx), UploadPost(ctx), Instagram(ctx))}


# ---- metadata per platform ---------------------------------------------------
def platform_metadata(clip: dict, platform: str, cfg) -> dict:
    title = (clip.get("title") or "").strip()
    credit = cfg.get("publishing.credit_template").format(streamer=clip["streamer"])
    tags = [t for t in (clip.get("hashtags") or []) if t]
    if platform == "youtube":
        title = title[:100]
        desc = f"{title}\n\n{credit}"
        tags = tags[:8]
    elif platform == "tiktok":
        desc = f"{title} {credit}"
        tags = tags[:5]
    elif platform == "instagram":
        desc = f"{title}\n\n{credit}"
        tags = tags[:5]          # Instagram allows at most 5 hashtags per post
    else:
        desc = f"{title}\n\n{credit}"
        tags = tags[:5]
    return {"title": title, "description": desc, "hashtags": tags}


# ---- quality -----------------------------------------------------------------
def quality_check(path: Path, platform: str) -> list[str]:
    problems = []
    if not path or not Path(path).exists():
        return ["video file missing"]
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "stream=codec_type,codec_name,width,height:format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=30).stdout
        import json
        d = json.loads(out)
    except Exception as e:  # noqa: BLE001
        return [f"unreadable video: {e}"]
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = float(d.get("format", {}).get("duration", 0))
    if not v or v.get("codec_name") != "h264":
        problems.append("video stream must be H.264")
    elif (v.get("width"), v.get("height")) != (1080, 1920):
        problems.append(f"resolution {v.get('width')}x{v.get('height')} is not 1080x1920")
    if not a:
        problems.append("no audio")
    if dur < 3:
        problems.append("shorter than 3 s")
    if dur > MAX_LEN.get(platform, 180):
        problems.append(f"longer than {MAX_LEN.get(platform, 180)} s for {platform}")
    return problems


class Engine:
    def __init__(self, cfg, db, vault, notifier=None):
        self.cfg, self.db, self.vault = cfg, db, vault
        self.ctx = Context(cfg, db, vault)
        self.providers = providers(self.ctx)
        self.notifier = notifier

    def _notify(self, event: str, text: str) -> None:
        if self.notifier:
            self.notifier.send(event, text)

    # ---- accounts --------------------------------------------------------------
    def save_accounts(self, provider: str, items: list[dict]) -> list[int]:
        now = time.time()
        p = self.cfg["publishing"]
        ids = []
        for it in items:
            tokens = it.pop("tokens", {}) or {}
            existing = self.db.one("SELECT id FROM accounts WHERE provider=? AND external_id=?",
                                   (provider, it["external_id"]))
            data = {k: v for k, v in it.items() if k in (
                "platform", "username", "display_name", "avatar_url", "token_expires_at",
                "refresh_expires_at", "settings")}
            data.update(tokens_enc=self.vault.encrypt(tokens), status="connected", status_message=None,
                        updated_at=now)
            if existing:
                self.db.update("accounts", existing["id"], data)
                ids.append(existing["id"])
            else:
                data.update(provider=provider, external_id=it["external_id"], created_at=now,
                            daily_limit=p["daily_limit"], min_interval_min=p["min_interval_min"],
                            auto_publish=0, dry_run=1, style="clean")
                ids.append(self.db.insert("accounts", data))
        return ids

    def account(self, account_id: int) -> dict | None:
        return self.db.one("SELECT * FROM accounts WHERE id=?", (account_id,))

    def verify_account(self, acc: dict) -> dict:
        prov = self.providers[acc["provider"]]
        try:
            upd = prov.verify(acc)
            upd.update(status="connected", updated_at=time.time())
            upd.setdefault("status_message", None)
        except AuthError as e:
            was = acc["status"]
            upd = {"status": "expired", "status_message": str(e)[:300], "updated_at": time.time()}
            if was == "connected":
                self._notify("account_problem", f"⚠️ {acc['provider']} account {acc.get('display_name')} "
                                                f"needs to be reconnected: {e}")
        except ProviderError as e:
            upd = {"status_message": f"check failed: {str(e)[:250]}", "updated_at": time.time()}
        self.db.update("accounts", acc["id"], upd)
        return upd

    # ---- planning ----------------------------------------------------------------
    def eligible(self, clip: dict, acc: dict, manual: bool = False) -> tuple[bool, str, float]:
        p = self.cfg["publishing"]
        if acc["status"] != "connected":
            return False, f"account {acc['status']}", 0
        if clip["status"] in ("rejected", "auto_rejected", "deleted"):
            return False, f"clip {clip['status']}", 0
        streamers = acc.get("streamers")
        if streamers and clip["streamer"] not in streamers:
            return False, "streamer not selected for this account", 0
        bonus = analytics.streamer_bonus(self.db, self.cfg, clip["streamer"]) if self.cfg.get("analytics.learning") else 0
        adjusted = round(clip["score"] + bonus, 1)
        if not manual:
            if not acc["auto_publish"]:
                return False, "auto-publish off", adjusted
            min_score = acc["min_score"] if acc.get("min_score") is not None else p["min_score"]
            if adjusted < min_score:
                return False, f"score {adjusted} below {min_score}", adjusted
            if p.get("require_rights"):
                s = self.db.one("SELECT rights_granted FROM streamers WHERE login=?", (clip["streamer"],))
                if not s or not s["rights_granted"]:
                    return False, "no publishing rights for this streamer", adjusted
        dup = self.duplicate_of(clip, acc)
        if dup:
            return False, dup, adjusted
        return True, "ok", adjusted

    def duplicate_of(self, clip: dict, acc: dict) -> str | None:
        rows = self.db.query(
            "SELECT j.id, c.id cid, c.streamer, c.src_start, c.src_end, c.peak_t, c.scores FROM publish_jobs j "
            "JOIN clips c ON c.id=j.clip_id WHERE j.account_id=? AND j.status NOT IN ('failed','cancelled','skipped','dry_run') "
            "AND j.created_at>?", (acc["id"], time.time() - 14 * 86400))
        fp = set((clip.get("scores") or {}).get("fingerprint", []) if isinstance(clip.get("scores"), dict) else [])
        for r in rows:
            if r["cid"] == clip["id"]:
                return f"already queued (job {r['id']})"
            if r["streamer"] == clip["streamer"] and r["src_start"] is not None and clip.get("src_start"):
                overlap = min(r["src_end"], clip["src_end"]) - max(r["src_start"], clip["src_start"])
                if overlap > 0.5 * min(clip["src_end"] - clip["src_start"], r["src_end"] - r["src_start"]):
                    return f"same moment as job {r['id']}"
            other = set((r["scores"] or {}).get("fingerprint", []) if isinstance(r["scores"], dict) else [])
            if fp and other and jaccard(fp, other) >= 0.6:
                return f"near-duplicate content of job {r['id']}"
        return None

    def next_slot(self, acc: dict, now: float | None = None, urgent: bool = False) -> float:
        now = now or time.time()
        p = self.cfg["publishing"]
        tz = ZoneInfo(p["timezone"])
        times = acc.get("posting_times") or p["posting_times"]
        if urgent:   # immediate / very high priority: first moment the limits allow
            times = "immediate"
        if times == "auto":
            times = analytics.best_times(self.db, self.cfg, acc) or p["posting_times"]
        limit = int(acc["daily_limit"] if acc.get("daily_limit") is not None else p["daily_limit"])
        gap = 60 * int(acc["min_interval_min"] if acc.get("min_interval_min") is not None else p["min_interval_min"])
        taken = [r["scheduled_at"] for r in self.db.query(
            f"SELECT scheduled_at FROM publish_jobs WHERE account_id=? AND status IN ({','.join('?' * len(COUNTED))}) "
            "AND scheduled_at IS NOT NULL", (acc["id"], *COUNTED))]
        if limit <= 0:
            raise ValueError("daily limit is 0")
        local_now = datetime.fromtimestamp(now, tz)
        for day in range(0, 30):
            d = (local_now + timedelta(days=day)).date()
            day_start = datetime.combine(d, datetime.min.time(), tz).timestamp()
            day_end = day_start + 86400
            used = sum(1 for t in taken if day_start <= t < day_end)
            if used >= limit:
                continue
            if times == "immediate":
                cands = [max(now, day_start)] + [t + gap for t in taken if day_start <= t + gap < day_end]
            else:
                cands = []
                for hm in times:
                    hh, mm = map(int, str(hm).split(":"))
                    cands.append(datetime.combine(d, datetime.min.time().replace(hour=hh, minute=mm), tz).timestamp())
            for c in sorted(cands):
                if c < now - 60 or not (day_start <= c < day_end):
                    continue
                if all(abs(c - t) >= gap for t in taken):
                    return max(c, now)
        raise ValueError("no free slot in the next 30 days")

    def plan_clip(self, clip_id: int, account_ids: list[int] | None = None, manual: bool = False) -> list[dict]:
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            return []
        accs = self.db.query("SELECT * FROM accounts WHERE status != 'disconnected'")
        if account_ids is not None:
            accs = [a for a in accs if a["id"] in account_ids]
        out = []
        for acc in accs:
            if self.db.one("SELECT id FROM publish_jobs WHERE clip_id=? AND account_id=?", (clip_id, acc["id"])):
                continue
            ok, why, adjusted = self.eligible(clip, acc, manual=manual)
            if not ok:
                if manual:
                    out.append({"account_id": acc["id"], "skipped": why})
                continue
            style = analytics.pick_style(self.db, self.cfg, acc, clip_id) if acc.get("style") == "auto" else acc.get("style")
            md = platform_metadata(clip, acc["platform"], self.cfg)
            needs_approval = self.providers[acc["provider"]].requires_approval
            urgent = clip.get("tier") in ("immediate", "very_high")
            try:
                slot = self.next_slot(acc, urgent=urgent)
            except ValueError as e:
                out.append({"account_id": acc["id"], "skipped": str(e)})
                continue
            now = time.time()
            jid = self.db.insert("publish_jobs", {
                "clip_id": clip_id, "account_id": acc["id"],
                "status": "awaiting_approval" if needs_approval else "scheduled",
                "scheduled_at": slot, "title": md["title"], "description": md["description"],
                "hashtags": md["hashtags"], "options": {}, "style": style, "adjusted_score": adjusted,
                "public_token": secrets.token_urlsafe(24), "created_at": now, "updated_at": now})
            out.append({"account_id": acc["id"], "job_id": jid, "scheduled_at": slot,
                        "approval": needs_approval})
            log.info("clip %s planned for %s/%s at %s (job %s)", clip_id, acc["provider"],
                     acc.get("display_name"), datetime.fromtimestamp(slot).isoformat(timespec="minutes"), jid)
            if needs_approval:
                self._notify("approval_needed", f"📝 TikTok post waiting for your approval: "
                                                f"\"{md['title']}\" ({acc.get('display_name')})")
        return out

    def plan_recent(self) -> None:
        """Pick up clips that became eligible later (new account, changed rules, approvals)."""
        for c in self.db.query("SELECT id FROM clips WHERE created_at>? AND status IN ('pending','approved')",
                               (time.time() - 48 * 3600,)):
            self.plan_clip(c["id"])

    # ---- approval (TikTok and manual review) --------------------------------------
    def approval_form(self, job_id: int) -> dict:
        job = self._job(job_id)
        acc = self.account(job["account_id"])
        prov = self.providers[acc["provider"]]
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (job["clip_id"],))
        form = {"job": _public_job(job), "account": _public_account(acc), "clip_id": clip["id"],
                "duration": clip["duration"], "caption": f"{job['description']} " +
                " ".join(f"#{t}" for t in job.get("hashtags") or [])}
        if isinstance(prov, TikTok):
            form["creator"] = prov.creator_info(acc)  # fresh, as TikTok requires
        return form

    def approve(self, job_id: int, opts: dict) -> dict:
        job = self._job(job_id)
        if job["status"] != "awaiting_approval":
            raise ValueError(f"job is {job['status']}")
        acc = self.account(job["account_id"])
        prov = self.providers[acc["provider"]]
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (job["clip_id"],))
        if isinstance(prov, TikTok):
            problems = validate_approval(opts, prov.creator_info(acc), clip["duration"])
            if problems:
                raise ValueError("; ".join(problems))
        when = opts.get("when", "slot")
        sched = time.time() if when == "now" or (job["scheduled_at"] or 0) < time.time() else job["scheduled_at"]
        clean = {k: opts.get(k) for k in ("title", "privacy_level", "allow_comment", "allow_duet", "allow_stitch",
                                           "commercial", "brand_organic", "brand_content", "consent")}
        self.db.update("publish_jobs", job_id, {"status": "scheduled", "options": clean, "approved_at": time.time(),
                                                "scheduled_at": sched, "updated_at": time.time()})
        return self._job(job_id)

    # ---- worker ----------------------------------------------------------------------
    def recover(self) -> None:
        n = self.db.execute("UPDATE publish_jobs SET status='scheduled', lease_until=NULL, updated_at=? "
                            "WHERE status='uploading'", (time.time(),)).rowcount
        if n:
            log.warning("recovered %d interrupted uploads (they resume from their saved state)", n)

    def tick(self) -> None:
        p = self.cfg["publishing"]
        now = time.time()
        for job in self.db.query("SELECT * FROM publish_jobs WHERE status='processing' AND "
                                 "(next_attempt_at IS NULL OR next_attempt_at<=?)", (now,)):
            self._check(job)
        if p.get("emergency_stop"):
            return
        due = self.db.query("SELECT * FROM publish_jobs WHERE status='scheduled' AND scheduled_at<=? AND "
                            "(next_attempt_at IS NULL OR next_attempt_at<=?) AND (lease_until IS NULL OR lease_until<?) "
                            "ORDER BY adjusted_score DESC, scheduled_at LIMIT 3", (now, now, now))
        # never let a slow upload delay an immediate-priority clip: highest score first
        for job in due:
            if self.cfg.get("publishing.emergency_stop"):
                break
            self._run(job)

    def _job(self, job_id: int) -> dict:
        j = self.db.one("SELECT * FROM publish_jobs WHERE id=?", (job_id,))
        if not j:
            raise ValueError("job not found")
        return j

    def _progress(self, job_id: int):
        def save(state: dict):
            self.db.update("publish_jobs", job_id, {"upload_state": state, "updated_at": time.time(),
                                                    "lease_until": time.time() + 1800})
        return save

    def _run(self, job: dict) -> None:
        acc = self.account(job["account_id"])
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (job["clip_id"],))
        if not acc or not clip:
            self._finish(job, "cancelled", error="account or clip removed")
            return
        if acc["status"] != "connected":
            self.db.update("publish_jobs", job["id"], {"next_attempt_at": time.time() + 3600,
                                                       "error": f"waiting: account {acc['status']}"})
            return
        prov = self.providers[acc["provider"]]
        # enforce the daily limit again at upload time (rules may have changed)
        tz = ZoneInfo(self.cfg.get("publishing.timezone"))
        d0 = datetime.combine(datetime.now(tz).date(), datetime.min.time(), tz).timestamp()
        done_today = self.db.one("SELECT COUNT(*) n FROM publish_jobs WHERE account_id=? AND status IN "
                                 "('processing','published') AND published_at>=?", (acc["id"], d0))["n"]
        if done_today >= int(acc["daily_limit"]):
            nxt = self.next_slot(acc, now=d0 + 86400)
            self.db.update("publish_jobs", job["id"], {"scheduled_at": nxt, "updated_at": time.time()})
            return
        self.db.update("publish_jobs", job["id"], {"status": "uploading", "lease_until": time.time() + 1800,
                                                   "updated_at": time.time()})
        dry = bool(self.cfg.get("publishing.dry_run") or acc["dry_run"])
        try:
            video = editor.ensure_variant(self.cfg, self.db, clip, job.get("style") or acc.get("style"))
            problems = quality_check(video, acc["platform"])
            if problems:
                raise Permanent("quality check failed: " + "; ".join(problems))
            public_url = None
            if prov.needs_public_url:
                base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
                if not base.startswith("https://") and not dry:
                    raise Permanent("PUBLIC_BASE_URL must be an https:// address for this provider")
                public_url = f"{base}/public/j/{job['public_token']}.mp4"
            opts = dict(job.get("options") or {})
            opts["video_path"] = str(video)
            self.db.update("publish_jobs", job["id"], {"options": opts})
            job["options"], job["duration"] = opts, clip["duration"]
            if dry:
                prov.verify(acc)  # real auth check: proves the account would work
                self._finish(job, "dry_run", error=None,
                             note=f"dry run OK: would upload {Path(video).name} to {acc['provider']}/"
                                  f"{acc.get('display_name')}")
                return
            res = prov.publish(job, acc, Path(video), public_url, self._progress(job["id"]))
            self.db.update("publish_jobs", job["id"], {
                "status": "processing", "remote_id": res.remote_id, "url": res.url, "lease_until": None,
                "upload_state": res.upload_state or (self._job(job["id"]).get("upload_state") or {}),
                "published_at": time.time(), "next_attempt_at": time.time() + 30, "error": None,
                "updated_at": time.time()})
            log.info("job %s uploaded to %s (%s)", job["id"], acc["provider"], res.remote_id)
        except AuthError as e:
            self.db.update("accounts", acc["id"], {"status": "expired", "status_message": str(e)[:300]})
            self.db.update("publish_jobs", job["id"], {"status": "scheduled", "lease_until": None,
                                                       "next_attempt_at": time.time() + 3600, "error": str(e)[:500]})
            self._notify("account_problem", f"⚠️ {acc['provider']} {acc.get('display_name')}: {e}")
        except Transient as e:
            self._retry(job, str(e), e.retry_at)
        except (Permanent, FileNotFoundError) as e:
            self._finish(job, "failed", error=str(e))
            self._notify("upload_failed", f"❌ Upload failed ({acc['provider']} {acc.get('display_name')}): {e}")
        except Exception as e:  # noqa: BLE001 - unknown errors are retried, never lost
            log.exception("job %s crashed", job["id"])
            self._retry(job, f"unexpected error: {e}", None)

    def _retry(self, job: dict, msg: str, retry_at: float | None) -> None:
        attempts = job["attempts"] + 1
        if attempts >= int(self.cfg.get("publishing.max_attempts")):
            self._finish(job, "failed", error=f"gave up after {attempts} attempts: {msg}", attempts=attempts)
            self._notify("upload_failed", f"❌ Upload gave up after {attempts} attempts: {msg}")
            return
        delay = min(6 * 3600, 60 * (2 ** attempts))  # 2, 4, 8, 16 ... minutes, max 6 h
        nxt = max(retry_at or 0, time.time() + delay)
        self.db.update("publish_jobs", job["id"], {"status": "scheduled", "attempts": attempts, "lease_until": None,
                                                   "next_attempt_at": nxt, "error": msg[:500],
                                                   "updated_at": time.time()})
        log.warning("job %s: %s (retry %d at %s)", job["id"], msg, attempts,
                    datetime.fromtimestamp(nxt).isoformat(timespec="minutes"))

    def _check(self, job: dict) -> None:
        acc = self.account(job["account_id"])
        prov = self.providers[acc["provider"]]
        try:
            res = prov.check(job, acc)
        except AuthError as e:
            self.db.update("publish_jobs", job["id"], {"next_attempt_at": time.time() + 3600, "error": str(e)[:300]})
            return
        except ProviderError as e:
            self.db.update("publish_jobs", job["id"], {"next_attempt_at": time.time() + 300, "error": str(e)[:300]})
            return
        if res.state == "published":
            st = {**(job.get("upload_state") or {}), **(res.upload_state or {})}
            self._finish(job, "published", url=res.url or job.get("url"), remote_id=res.remote_id or job["remote_id"],
                         upload_state=st, note=res.message)
            self._notify("upload_success", f"✅ Published on {acc['provider']} ({acc.get('display_name')}): "
                                           f"{job['title']}\n{res.url or ''}")
        elif res.state == "failed":
            self._finish(job, "failed", error=res.message)
            self._notify("upload_failed", f"❌ {acc['provider']} rejected the post: {res.message}")
        else:
            age = time.time() - (job.get("published_at") or job["updated_at"])
            if age > 6 * 3600:
                self._finish(job, "failed", error=f"still processing after 6 h ({res.message})")
            else:
                self.db.update("publish_jobs", job["id"], {"next_attempt_at": time.time() + 60,
                                                           "error": None, "updated_at": time.time()})

    def _finish(self, job: dict, status: str, error: str | None = None, note: str | None = None, **cols) -> None:
        data = {"status": status, "lease_until": None, "updated_at": time.time(), "error": error or note, **cols}
        self.db.update("publish_jobs", job["id"], data)

    # ---- actions from the dashboard -------------------------------------------------
    def cancel(self, job_id: int) -> None:
        job = self._job(job_id)
        if job["status"] in ("uploading", "processing", "published"):
            raise ValueError(f"cannot cancel a job that is {job['status']}")
        self._finish(job, "cancelled")

    def retry_now(self, job_id: int) -> None:
        job = self._job(job_id)
        if job["status"] not in ("failed", "scheduled", "dry_run"):
            raise ValueError(f"cannot retry a job that is {job['status']}")
        self.db.update("publish_jobs", job_id, {"status": "scheduled", "scheduled_at": time.time(),
                                                "next_attempt_at": None, "attempts": 0, "error": None,
                                                "updated_at": time.time()})


def _public_job(j: dict) -> dict:
    return {k: v for k, v in j.items() if k not in ("public_token", "upload_state")}


def _public_account(a: dict) -> dict:
    return {k: v for k, v in a.items() if k != "tokens_enc"}


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:80]


def humanize(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


_ = math  # noqa: F401
