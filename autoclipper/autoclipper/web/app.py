"""Dashboard + REST API (FastAPI). Protected with HTTP Basic auth."""
from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from ..config import Config
from ..db import DB
from ..manager import Manager

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
security = HTTPBasic()

# settings the dashboard may change (everything else stays file-only)
EDITABLE = {"live_check_interval_sec", "recording", "chat", "detection", "clip", "scoring", "whisper",
            "render", "subtitles", "metadata", "publishing", "storage"}


class StreamerIn(BaseModel):
    login: str
    platform: str = "twitch"
    rights_granted: bool = False


class StreamerPatch(BaseModel):
    enabled: bool | None = None
    rights_granted: bool | None = None


class PublishIn(BaseModel):
    platform: str
    confirm_rights: bool = False


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
        pubs = db.query("SELECT * FROM publications ORDER BY id DESC LIMIT 1000")
        for r in rows:
            r["publications"] = [x for x in pubs if x["clip_id"] == r["id"]]
            if isinstance(r.get("scores"), dict):
                r["scores"].pop("fingerprint", None)
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

    @app.post("/api/clips/{clip_id}/publish", dependencies=A)
    def publish(clip_id: int, body: PublishIn, request: Request):
        try:
            pid = mgr(request).request_publish(clip_id, body.platform, body.confirm_rights)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"publication_id": pid}

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

    # Unauthenticated, unguessable per-clip URL so Instagram can fetch an approved video.
    @app.get("/public/{token}.mp4")
    def public_clip(token: str):
        c = db.one("SELECT * FROM clips WHERE public_token=? AND status IN ('approved','published')", (token,))
        if not c or not c["path"] or not os.path.exists(c["path"]):
            raise HTTPException(404)
        return FileResponse(c["path"], media_type="video/mp4")

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
