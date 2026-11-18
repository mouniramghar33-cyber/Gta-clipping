"""YouTube Shorts via YouTube Data API v3 (videos.insert, resumable upload).

Env: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN (scope youtube.upload).
A vertical video of 3 minutes or less is classified as a Short by YouTube.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

from . import Publisher, PublishError, caption_for, wait

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubePublisher(Publisher):
    name = "youtube"

    def configured(self) -> bool:
        return all(os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))

    def _access_token(self) -> str:
        r = httpx.post(TOKEN_URL, data={
            "client_id": os.environ["YT_CLIENT_ID"], "client_secret": os.environ["YT_CLIENT_SECRET"],
            "refresh_token": os.environ["YT_REFRESH_TOKEN"], "grant_type": "refresh_token"}, timeout=30)
        if r.status_code != 200:
            raise PublishError(f"YouTube token refresh failed: {r.status_code} {r.text[:300]}")
        return r.json()["access_token"]

    def publish(self, clip, video: Path, public_url, cfg):
        token = self._access_token()
        size = video.stat().st_size
        credit = cfg.get("publishing.credit_template").format(streamer=clip["streamer"])
        desc = caption_for(clip, credit, 4900) + "\n#Shorts"
        body = {
            "snippet": {"title": clip["title"][:100], "description": desc,
                        "tags": (clip.get("hashtags") or [])[:15], "categoryId": "20"},
            "status": {"privacyStatus": cfg.get("publishing.youtube_privacy"),
                       "selfDeclaredMadeForKids": False},
        }
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
             "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"}
        r = httpx.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                       headers=h, json=body, timeout=60)
        if r.status_code != 200 or "location" not in r.headers:
            raise PublishError(f"YouTube upload init failed: {r.status_code} {r.text[:300]}")
        session = r.headers["location"]
        offset, backoff = 0, 2
        for _ in range(8):
            with video.open("rb") as f:
                f.seek(offset)
                data = f.read()
            hdr = {"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"}
            if offset:
                hdr["Content-Range"] = f"bytes {offset}-{size - 1}/{size}"
            try:
                r = httpx.put(session, headers=hdr, content=data, timeout=600)
            except httpx.TransportError:
                r = None
            if r is not None and r.status_code in (200, 201):
                vid = r.json()["id"]
                return vid, f"https://youtube.com/shorts/{vid}"
            if r is not None and r.status_code == 404:
                raise PublishError("YouTube upload session expired")
            if r is not None and r.status_code not in (308, 500, 502, 503, 504):
                raise PublishError(f"YouTube upload failed: {r.status_code} {r.text[:300]}")
            wait(backoff)
            backoff = min(backoff * 2, 60)
            st = httpx.put(session, headers={"Authorization": f"Bearer {token}", "Content-Length": "0",
                                             "Content-Range": f"bytes */{size}"}, timeout=60)
            if st.status_code in (200, 201):
                vid = st.json()["id"]
                return vid, f"https://youtube.com/shorts/{vid}"
            rng = st.headers.get("range")
            offset = int(rng.split("-")[1]) + 1 if rng else 0
        raise PublishError("YouTube upload failed after retries")
