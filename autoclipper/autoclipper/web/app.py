"""Dashboard + REST API (FastAPI). Protected with HTTP Basic auth."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from urllib.parse import quote
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .. import analytics, editor
from ..config import Config
from ..db import DB
from ..manager import Manager

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
security = HTTPBasic()

# settings the dashboard may change (everything else stays file-only)
EDITABLE = {"live_check_interval_sec", "recording", "chat", "detection", "clip", "scoring", "whisper",
            "render", "subtitles", "metadata", "publishing", "styles", "notifications", "analytics", "storage",
            "gta6", "resources", "watchlist"}


class StreamerIn(BaseModel):
    login: str
    platform: str = "twitch"
    rights_granted: bool = False


class StreamerPatch(BaseModel):
    enabled: bool | None = None
    rights_granted: bool | None = None


class SendIn(BaseModel):
    account_ids: list[int]
    confirm_rights: bool = False


class AccountPatch(BaseModel):
    auto_publish: bool | None = None
    dry_run: bool | None = None
    daily_limit: int | None = None
    min_interval_min: int | None = None
    posting_times: list[str] | str | None = None
    style: str | None = None
    streamers: list[str] | None = None
    min_score: float | None = None
    privacy: str | None = None
    ai_disclosure: bool | None = None


class ApprovalIn(BaseModel):
    title: str
    privacy_level: str | None = None
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    commercial: bool = False
    brand_organic: bool = False
    brand_content: bool = False
    consent: bool = False
    when: str = "slot"


class InstagramIn(BaseModel):
    user_id: str
    token: str


class ClipPatch(BaseModel):
    title: str | None = None
    hook: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None


def create_app(data_dir: str) -> FastAPI:
    cfg = Config(data_dir)
    db = DB(cfg.db_path)
    user = os.environ.get("DASHBOARD_USER", "admin")
    password = os.environ.get("DASHBOARD_PASSWORD")
    if not password:
        raise RuntimeError("Set DASHBOARD_PASSWORD (the dashboard is never exposed without a password)")

    def auth(c: HTTPBasicCredentials = Depends(security)) -> None:
        ok = secrets.compare_digest(c.username.encode(), user.encode()) and \
            secrets.compare_digest(c.password.encode(), password.encode())
        if not ok:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Basic"})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mgr = Manager(cfg, db)
        app.state.mgr = mgr
        await mgr.start()
        yield
        await mgr.stop()

    app = FastAPI(title="AutoClipper", lifespan=lifespan)
    A = [Depends(auth)]

    def mgr(request: Request) -> Manager:
        return request.app.state.mgr

    @app.get("/healthz")
    def health(request: Request):
        m = mgr(request)
        return {"ok": all(not t.done() for t in m.tasks), "chat": m.chat.connected}

    @app.get("/", response_class=HTMLResponse, dependencies=A)
    def index():
        return (STATIC / "index.html").read_text()

    # ---- status -----------------------------------------------------------
    @app.get("/api/status", dependencies=A)
    def status(request: Request):
        return mgr(request).status()

    @app.get("/api/chat/{login}", dependencies=A)
    def chat(login: str, minutes: int = 30):
        bins = db.chat_bins(login.lower(), time.time() - minutes * 60)
        moments = db.query("SELECT id, t_start, t_end, peak_t, peak_ratio, status FROM moments "
                           "WHERE streamer=? AND t_end>=?", (login.lower(), time.time() - minutes * 60))
        return {"bins": bins, "moments": moments}

    # ---- streamers --------------------------------------------------------
    @app.get("/api/streamers", dependencies=A)
    def streamers():
        return db.streamers()

    @app.post("/api/streamers", dependencies=A)
    def add_streamer(body: StreamerIn, request: Request):
        try:
            mgr(request).add_streamer(body.login, body.platform, body.rights_granted)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.patch("/api/streamers/{login}", dependencies=A)
    def patch_streamer(login: str, body: StreamerPatch):
        if body.enabled is not None:
            db.execute("UPDATE streamers SET enabled=? WHERE login=?", (int(body.enabled), login))
        if body.rights_granted is not None:
            db.execute("UPDATE streamers SET rights_granted=? WHERE login=?", (int(body.rights_granted), login))
        return {"ok": True}

    @app.delete("/api/streamers/{login}", dependencies=A)
    async def delete_streamer(login: str, request: Request):
        await mgr(request).remove_streamer(login)
        return {"ok": True}

    # ---- moments & clips ----------------------------------------------------
    @app.get("/api/moments", dependencies=A)
    def moments(limit: int = 100):
        return db.query("SELECT * FROM moments ORDER BY id DESC LIMIT ?", (min(limit, 500),))

    @app.get("/api/clips", dependencies=A)
    def clips(status: str | None = None, min_score: float = 0, limit: int = 100):
        q = "SELECT * FROM clips WHERE score>=?"
        p: list = [min_score]
        if status:
            q += " AND status=?"
            p.append(status)
        q += " ORDER BY priority DESC, created_at DESC LIMIT ?"
        p.append(min(limit, 500))
        rows = db.query(q, p)
        jobs = db.query("SELECT j.clip_id, j.status, j.url, j.error, a.display_name FROM publish_jobs j "
                        "JOIN accounts a ON a.id=j.account_id ORDER BY j.id DESC LIMIT 2000")
        for r in rows:
            r["jobs"] = [x for x in jobs if x["clip_id"] == r["id"]]
            if isinstance(r.get("scores"), dict):
                r["scores"].pop("fingerprint", None)
            if isinstance(r.get("labels"), str):
                try:
                    r["labels"] = __import__("json").loads(r["labels"])
                except ValueError:
                    r["labels"] = None
            r.pop("public_token", None)
        return rows

    def _clip(clip_id: int) -> dict:
        c = db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not c:
            raise HTTPException(404, "clip not found")
        return c

    @app.post("/api/clips/{clip_id}/approve", dependencies=A)
    def approve(clip_id: int):
        _clip(clip_id)
        db.update("clips", clip_id, {"status": "approved", "decided_at": time.time()})
        return {"ok": True}

    @app.post("/api/clips/{clip_id}/reject", dependencies=A)
    def reject(clip_id: int):
        _clip(clip_id)
        db.update("clips", clip_id, {"status": "rejected", "decided_at": time.time()})
        return {"ok": True}

    @app.patch("/api/clips/{clip_id}", dependencies=A)
    def edit_clip(clip_id: int, body: ClipPatch):
        _clip(clip_id)
        data = {k: v for k, v in body.model_dump().items() if v is not None}
        if data:
            db.update("clips", clip_id, data)
        return {"ok": True}

    @app.post("/api/clips/{clip_id}/send", dependencies=A)
    def send_clip(clip_id: int, body: SendIn, request: Request):
        """Manually queue a clip for selected accounts (bypasses auto-publish/min score)."""
        if not body.confirm_rights:
            raise HTTPException(400, "confirm that you have the rights/permission to publish this content")
        c = _clip(clip_id)
        if c["status"] in ("rejected", "auto_rejected", "deleted"):
            db.update("clips", clip_id, {"status": "approved", "decided_at": time.time()})
        return {"results": mgr(request).engine.plan_clip(clip_id, body.account_ids, manual=True)}

    @app.post("/api/clips/{clip_id}/variant/{style}", dependencies=A)
    async def make_variant(clip_id: int, style: str, request: Request):
        c = _clip(clip_id)
        if style not in (cfg.get("styles") or {}):
            raise HTTPException(400, "unknown style")
        try:
            path = await asyncio.to_thread(editor.ensure_variant, cfg, db, c, style)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        return {"url": f"/media/variant/{clip_id}/{style}.mp4", "file": Path(path).name}

    @app.get("/media/variant/{clip_id}/{style}.mp4", dependencies=A)
    def variant_file(clip_id: int, style: str):
        v = db.one("SELECT path FROM clip_variants WHERE clip_id=? AND style=?", (clip_id, style))
        if not v or not os.path.exists(v["path"]):
            raise HTTPException(404, "variant not rendered")
        return FileResponse(v["path"], media_type="video/mp4")

    @app.get("/media/clip/{clip_id}.mp4", dependencies=A)
    def clip_file(clip_id: int, download: int = 0):
        c = _clip(clip_id)
        if not c["path"] or not os.path.exists(c["path"]):
            raise HTTPException(404, "file deleted")
        name = Path(c["path"]).name if download else None
        return FileResponse(c["path"], media_type="video/mp4", filename=name)

    @app.get("/media/thumb/{clip_id}.jpg", dependencies=A)
    def thumb(clip_id: int):
        c = _clip(clip_id)
        if not c["thumb"] or not os.path.exists(c["thumb"]):
            raise HTTPException(404, "no thumbnail")
        return FileResponse(c["thumb"], media_type="image/jpeg")

    # ---- public video links for providers that download the file (Buffer, Instagram) ----
    @app.get("/public/j/{token}.mp4")
    def public_job_video(token: str):
        j = db.one("SELECT * FROM publish_jobs WHERE public_token=?", (token,))
        days = float(cfg.get("publishing.public_link_days") or 7)
        if not j or j["status"] not in ("uploading", "processing", "published", "scheduled") \
                or time.time() - j["created_at"] > days * 86400:
            raise HTTPException(404)
        path = (j.get("options") or {}).get("video_path")
        if not path or not os.path.exists(path):
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4")

    # ---- Kick chat webhooks (official Kick Public API events; signature-verified) ----
    @app.post("/webhooks/kick")
    async def kick_webhook(request: Request):
        body = await request.body()
        code, msg = await asyncio.to_thread(mgr(request).kick.handle_webhook, dict(request.headers), body)
        return JSONResponse({"result": msg}, status_code=code)

    # ---- watchlist / streamer stats ---------------------------------------------
    @app.get("/api/streamer-stats", dependencies=A)
    def streamer_stats():
        day = time.time() - 86400
        out = []
        for r in db.query("SELECT login, name, rights_granted, enabled, weight, source FROM streamers ORDER BY login"):
            q = lambda sql, *p: db.one(sql, p)["n"]  # noqa: E731
            views = db.one("""SELECT SUM(v) n FROM (SELECT (SELECT views FROM metrics WHERE job_id=j.id ORDER BY fetched_at DESC LIMIT 1) v
                              FROM publish_jobs j JOIN clips c ON c.id=j.clip_id WHERE c.streamer=? AND j.status='published')""",
                           (r["login"],))["n"]
            out.append({**r, "channels": db.channels(r["login"]),
                        "moments_24h": q("SELECT COUNT(*) n FROM moments WHERE streamer=? AND created_at>?", r["login"], day),
                        "clips_24h": q("SELECT COUNT(*) n FROM clips WHERE streamer=? AND created_at>?", r["login"], day),
                        "clips_total": q("SELECT COUNT(*) n FROM clips WHERE streamer=?", r["login"]),
                        "gta_clips": q("SELECT COUNT(*) n FROM clips WHERE streamer=? AND gta=1", r["login"]),
                        "published": q("SELECT COUNT(*) n FROM publish_jobs j JOIN clips c ON c.id=j.clip_id WHERE c.streamer=? AND j.status='published'", r["login"]),
                        "avg_score": db.one("SELECT AVG(score) n FROM clips WHERE streamer=?", (r["login"],))["n"],
                        "avg_latency": db.one("SELECT AVG(latency) n FROM clips WHERE streamer=? AND latency IS NOT NULL", (r["login"],))["n"],
                        "views": views})
        return out

    @app.get("/api/overview", dependencies=A)
    def overview():
        q = lambda sql, *p: db.one(sql, p)["n"]  # noqa: E731
        views = db.one("""SELECT SUM(v) n FROM (SELECT (SELECT views FROM metrics WHERE job_id=j.id ORDER BY fetched_at DESC LIMIT 1) v
                          FROM publish_jobs j WHERE j.status='published')""")["n"]
        return {"published": q("SELECT COUNT(*) n FROM publish_jobs WHERE status='published'"),
                "scheduled": q("SELECT COUNT(*) n FROM publish_jobs WHERE status IN ('scheduled','awaiting_approval')"),
                "failed": q("SELECT COUNT(*) n FROM publish_jobs WHERE status='failed'"),
                "clips_24h": q("SELECT COUNT(*) n FROM clips WHERE created_at>?", time.time() - 86400),
                "gta_clips_24h": q("SELECT COUNT(*) n FROM clips WHERE gta=1 AND created_at>?", time.time() - 86400),
                "avg_latency": db.one("SELECT AVG(latency) n FROM clips WHERE latency IS NOT NULL AND created_at>?",
                                      (time.time() - 86400,))["n"],
                "total_views": views, "gta6": cfg.get("gta6"),
                "chat_sources": {"kick_configured": bool(getattr(app.state, "mgr", None) and app.state.mgr.kick.configured()),
                                 "youtube_configured": bool(getattr(app.state, "mgr", None) and app.state.mgr.yt_chat.configured())}}

    # ---- social accounts -------------------------------------------------------
    def eng(request: Request):
        return mgr(request).engine

    def base_url(request: Request) -> str:
        return (os.environ.get("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")

    def public_acc(a: dict) -> dict:
        a = {k: v for k, v in a.items() if k != "tokens_enc"}
        a["jobs_today"] = db.one("SELECT COUNT(*) n FROM publish_jobs WHERE account_id=? AND status IN "
                                 "('processing','published') AND published_at>?", (a["id"], time.time() - 86400))["n"]
        return a

    @app.get("/api/accounts", dependencies=A)
    def list_accounts():
        return [public_acc(a) for a in db.query("SELECT * FROM accounts WHERE status!='disconnected' ORDER BY id")]

    @app.get("/oauth/{provider}/start", dependencies=A)
    def oauth_start(provider: str, request: Request):
        prov = eng(request).providers.get(provider)
        if not prov or not prov.oauth:
            raise HTTPException(404, "unknown OAuth provider")
        if not prov.configured():
            raise HTTPException(400, prov.setup_hint())
        state = secrets.token_urlsafe(24)
        url, verifier = prov.oauth_start(f"{base_url(request)}/oauth/{provider}/callback", state)
        db.execute("DELETE FROM oauth_states WHERE created_at<?", (time.time() - 900,))
        db.execute("INSERT INTO oauth_states (state, provider, verifier, created_at) VALUES (?,?,?,?)",
                   (state, provider, verifier, time.time()))
        return RedirectResponse(url, status_code=302)

    # Not behind basic auth (the platform redirects here), protected by a single-use state value.
    @app.get("/oauth/{provider}/callback")
    def oauth_callback(provider: str, request: Request, code: str = "", state: str = "", error: str = "",
                       error_description: str = ""):
        row = db.one("SELECT * FROM oauth_states WHERE state=? AND provider=?", (state, provider))
        if row:
            db.execute("DELETE FROM oauth_states WHERE state=?", (state,))
        if not row or time.time() - row["created_at"] > 900:
            return RedirectResponse("/#accounts?error=" + quote("login expired, try again"), status_code=302)
        if error or not code:
            return RedirectResponse("/#accounts?error=" + quote(error_description or error or "cancelled"), status_code=302)
        e = eng(request)
        try:
            items = e.providers[provider].oauth_finish(code, f"{base_url(request)}/oauth/{provider}/callback",
                                                       row["verifier"])
            e.save_accounts(provider, items)
        except Exception as ex:  # noqa: BLE001
            log.warning("oauth %s failed: %s", provider, ex)
            return RedirectResponse("/#accounts?error=" + quote(str(ex)[:200]), status_code=302)
        return RedirectResponse("/#accounts?connected=" + quote(provider), status_code=302)

    @app.post("/api/accounts/import/{provider}", dependencies=A)
    def import_accounts(provider: str, request: Request, body: InstagramIn | None = None):
        e = eng(request)
        prov = e.providers.get(provider)
        if not prov or prov.oauth:
            raise HTTPException(404, "unknown provider")
        try:
            items = prov.connect(**(body.model_dump() if body else {}))
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(400, str(ex)[:300])
        if not items:
            raise HTTPException(400, "no connected channels found")
        return {"imported": len(e.save_accounts(provider, items))}

    @app.patch("/api/accounts/{acc_id}", dependencies=A)
    def patch_account(acc_id: int, body: AccountPatch):
        if not db.one("SELECT id FROM accounts WHERE id=?", (acc_id,)):
            raise HTTPException(404)
        data = {k: v for k, v in body.model_dump().items() if v is not None}
        if "style" in data and data["style"] != "auto" and data["style"] not in (cfg.get("styles") or {}):
            raise HTTPException(400, "unknown style")
        pt = data.get("posting_times")
        if isinstance(pt, str) and pt not in ("auto", "immediate"):
            raise HTTPException(400, "posting_times must be a list of HH:MM, 'auto' or 'immediate'")
        if isinstance(pt, list):
            for t in pt:
                hh, _, mm = t.partition(":")
                if not (hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
                    raise HTTPException(400, f"invalid time {t}")
        for k in ("auto_publish", "dry_run", "ai_disclosure"):
            if k in data:
                data[k] = int(data[k])
        if data.get("daily_limit", 1) < 0 or data.get("min_interval_min", 0) < 0:
            raise HTTPException(400, "limits must be positive")
        data["updated_at"] = time.time()
        db.update("accounts", acc_id, data)
        return {"ok": True}

    @app.post("/api/accounts/{acc_id}/verify", dependencies=A)
    def verify_account(acc_id: int, request: Request):
        a = db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
        if not a:
            raise HTTPException(404)
        return eng(request).verify_account(a)

    @app.delete("/api/accounts/{acc_id}", dependencies=A)
    def disconnect_account(acc_id: int, request: Request):
        a = db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
        if not a:
            raise HTTPException(404)
        e = eng(request)
        tokens = e.vault.decrypt(a["tokens_enc"]) if a["tokens_enc"] else {}
        try:  # revoke at the platform where an official revoke endpoint exists (best effort)
            import httpx
            if a["provider"] == "youtube" and tokens.get("refresh_token"):
                httpx.post("https://oauth2.googleapis.com/revoke", data={"token": tokens["refresh_token"]}, timeout=15)
            if a["provider"] == "tiktok" and tokens.get("access_token"):
                httpx.post("https://open.tiktokapis.com/v2/oauth/revoke/", timeout=15, data={
                    "client_key": e.vault.get("TIKTOK_CLIENT_KEY"), "client_secret": e.vault.get("TIKTOK_CLIENT_SECRET"),
                    "token": tokens["access_token"]})
        except Exception as ex:  # noqa: BLE001
            log.warning("token revoke failed: %s", ex)
        db.update("accounts", acc_id, {"status": "disconnected", "tokens_enc": None, "auto_publish": 0,
                                       "updated_at": time.time()})
        db.execute("UPDATE publish_jobs SET status='cancelled', error='account disconnected' WHERE account_id=? "
                   "AND status IN ('awaiting_approval','scheduled')", (acc_id,))
        return {"ok": True}

    # ---- publishing queue -------------------------------------------------------
    @app.get("/api/jobs", dependencies=A)
    def list_jobs(status: str | None = None, limit: int = 200):
        q = ("SELECT j.*, c.streamer, c.score, c.duration, c.thumb, a.provider, a.platform, a.display_name, "
             "a.avatar_url FROM publish_jobs j JOIN clips c ON c.id=j.clip_id JOIN accounts a ON a.id=j.account_id")
        p: list = []
        if status:
            q += " WHERE j.status=?"
            p.append(status)
        q += " ORDER BY CASE j.status WHEN 'awaiting_approval' THEN 0 WHEN 'uploading' THEN 1 WHEN 'processing' THEN 2 " \
             "WHEN 'scheduled' THEN 3 ELSE 4 END, j.scheduled_at DESC LIMIT ?"
        p.append(min(limit, 1000))
        rows = db.query(q, p)
        for r in rows:
            r.pop("public_token", None)
            st = r.pop("upload_state", None) or {}
            r["progress"] = round(100 * st["uploaded"] / st["size"]) if st.get("size") else None
        return rows

    @app.get("/api/jobs/{job_id}/approval", dependencies=A)
    def approval_form(job_id: int, request: Request):
        try:
            return eng(request).approval_form(job_id)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(400, str(ex)[:300])

    @app.post("/api/jobs/{job_id}/approve", dependencies=A)
    def approve_job(job_id: int, body: ApprovalIn, request: Request):
        try:
            j = eng(request).approve(job_id, body.model_dump())
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(400, str(ex)[:300])
        return {"status": j["status"], "scheduled_at": j["scheduled_at"]}

    @app.post("/api/jobs/{job_id}/cancel", dependencies=A)
    def cancel_job(job_id: int, request: Request):
        try:
            eng(request).cancel(job_id)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/retry", dependencies=A)
    def retry_job(job_id: int, request: Request):
        try:
            eng(request).retry_now(job_id)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        return {"ok": True}

    @app.post("/api/publishing/emergency-stop", dependencies=A)
    def emergency(body: dict):
        on = bool(body.get("on", True))
        cfg.update({"publishing": {"emergency_stop": on}})
        if on:
            log.warning("EMERGENCY STOP activated from the dashboard")
        return {"emergency_stop": on}

    # ---- analytics --------------------------------------------------------------
    @app.get("/api/analytics", dependencies=A)
    def get_analytics():
        return analytics.insights(db, cfg)

    @app.post("/api/analytics/refresh", dependencies=A)
    async def refresh_analytics(request: Request):
        n = await asyncio.to_thread(analytics.collect, eng(request))
        return {"stored": n}

    # ---- setup wizard ----------------------------------------------------------
    @app.get("/api/setup", dependencies=A)
    def setup_status(request: Request):
        e = eng(request)
        base = base_url(request)
        hb = db.kv_get("worker_heartbeat") or {}
        accs = db.query("SELECT provider, platform, status FROM accounts WHERE status!='disconnected'")
        return {
            "public_base_url": os.environ.get("PUBLIC_BASE_URL") or None,
            "https": base.startswith("https://"),
            "encryption": "env key" if os.environ.get("AUTOCLIPPER_SECRET_KEY") else "generated key file",
            "secrets": e.vault.status(),
            "redirect_uris": {p: f"{base}/oauth/{p}/callback" for p in ("youtube", "tiktok")},
            "kick_webhook": f"{base}/webhooks/kick",
            "kick_chat": mgr(request).kick.configured(), "youtube_chat": mgr(request).yt_chat.configured(),
            "providers": {k: {"configured": p.configured(), "hint": p.setup_hint(), "oauth": p.oauth,
                              "approval": p.requires_approval} for k, p in e.providers.items()},
            "accounts": accs,
            "telegram": mgr(request).notifier.configured(),
            "dry_run": cfg.get("publishing.dry_run"),
            "emergency_stop": cfg.get("publishing.emergency_stop"),
            "worker_alive": time.time() - hb.get("t", 0) < 180,
            "streamers_with_rights": db.one("SELECT COUNT(*) n FROM streamers WHERE rights_granted=1")["n"],
            "capcut": {"supported": False, "reason": "CapCut offers no API that renders and exports finished "
                       "videos on a Linux server: VectCutAPI only writes draft projects (cloud rendering is not "
                       "open source) and CapCut's MCP edits cloud drafts that must be exported in the CapCut "
                       "editor. AutoClipper's own FFmpeg editor renders everything instead."},
        }

    @app.put("/api/setup/secrets", dependencies=A)
    def set_secrets(body: dict, request: Request):
        v = mgr(request).vault
        try:
            for k, val in body.items():
                v.set(k, str(val).strip())
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        return v.status()

    @app.post("/api/setup/telegram-test", dependencies=A)
    def telegram_test(request: Request):
        ok, msg = mgr(request).notifier.test()
        if not ok:
            raise HTTPException(400, msg)
        return {"ok": True}

    @app.post("/api/setup/dry-run-test", dependencies=A)
    def dry_run_test(request: Request):
        """Queue the newest clip for every connected account; the worker then runs it (in dry-run
        mode when enabled) so the whole chain can be checked without posting anything."""
        c = db.one("SELECT id FROM clips WHERE path!='' ORDER BY id DESC LIMIT 1")
        if not c:
            raise HTTPException(400, "no clip available yet")
        accs = [a["id"] for a in db.query("SELECT id FROM accounts WHERE status='connected'")]
        if not accs:
            raise HTTPException(400, "connect an account first")
        if not cfg.get("publishing.dry_run"):
            raise HTTPException(400, "global dry-run is off; turn it on for a test")
        return {"clip_id": c["id"], "results": eng(request).plan_clip(c["id"], accs, manual=True)}

    # ---- settings / storage / logs ----------------------------------------------
    @app.get("/api/settings", dependencies=A)
    def get_settings():
        d = cfg.as_dict()
        return {k: v for k, v in d.items() if k in EDITABLE}

    @app.put("/api/settings", dependencies=A)
    def put_settings(patch: dict):
        bad = set(patch) - EDITABLE
        if bad:
            raise HTTPException(400, f"not editable: {sorted(bad)}")
        try:
            new = cfg.update(patch)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, str(e))
        return {k: v for k, v in new.items() if k in EDITABLE}

    @app.post("/api/storage/cleanup", dependencies=A)
    async def cleanup(request: Request):
        import asyncio
        return await asyncio.to_thread(mgr(request).cleanup)

    @app.get("/api/logs", dependencies=A)
    def logs(lines: int = 200):
        p = cfg.data_dir / "logs" / "autoclipper.log"
        if not p.exists():
            return JSONResponse({"lines": []})
        with p.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            data = f.read().decode(errors="replace").splitlines()
        return {"lines": data[-min(lines, 2000):]}

    return app
