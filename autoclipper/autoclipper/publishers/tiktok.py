"""TikTok Content Posting API, Direct Post with FILE_UPLOAD.

Env: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN (scope video.publish).
Until TikTok audits the app, posts can only be private (SELF_ONLY).
Refresh tokens rotate; the newest one is stored in <data>/secrets/tokens.json.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

from . import Publisher, PublishError, TokenStore, caption_for, wait

API = "https://open.tiktokapis.com/v2"
MB = 1024 * 1024


def chunk_plan(size: int, chunk: int = 10 * MB) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok's media transfer rules."""
    if size < 5 * MB:
        return size, 1
    count = max(1, size // chunk)
    return chunk, count


class TikTokPublisher(Publisher):
    name = "tiktok"

    def __init__(self, store: TokenStore):
        self.store = store

    def configured(self) -> bool:
        return bool(os.environ.get("TIKTOK_CLIENT_KEY") and os.environ.get("TIKTOK_CLIENT_SECRET")
                    and (self.store.get("tiktok_refresh_token") or os.environ.get("TIKTOK_REFRESH_TOKEN")))

    def _access_token(self) -> str:
        refresh = self.store.get("tiktok_refresh_token") or os.environ["TIKTOK_REFRESH_TOKEN"]
        r = httpx.post(f"{API}/oauth/token/", data={
            "client_key": os.environ["TIKTOK_CLIENT_KEY"], "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
            "grant_type": "refresh_token", "refresh_token": refresh},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        d = r.json()
        if r.status_code != 200 or "access_token" not in d:
            raise PublishError(f"TikTok token refresh failed: {r.status_code} {str(d)[:300]}")
        if d.get("refresh_token"):
            self.store.set("tiktok_refresh_token", d["refresh_token"])
        return d["access_token"]

    @staticmethod
    def _check(r: httpx.Response, what: str) -> dict:
        try:
            d = r.json()
        except ValueError:
            raise PublishError(f"TikTok {what}: HTTP {r.status_code}")
        if (d.get("error") or {}).get("code") not in (None, "ok"):
            raise PublishError(f"TikTok {what}: {d['error'].get('code')} {d['error'].get('message')}")
        return d.get("data") or {}

    def publish(self, clip, video: Path, public_url, cfg):
        token = self._access_token()
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        info = self._check(httpx.post(f"{API}/post/publish/creator_info/query/", headers=h, timeout=30),
                           "creator_info")
        if clip.get("duration") and info.get("max_video_post_duration_sec") \
                and clip["duration"] > info["max_video_post_duration_sec"]:
            raise PublishError("clip longer than the creator's maximum post duration")
        privacy = cfg.get("publishing.tiktok_privacy")
        options = info.get("privacy_level_options") or ["SELF_ONLY"]
        if privacy not in options:
            privacy = "SELF_ONLY" if "SELF_ONLY" in options else options[0]
        size = video.stat().st_size
        chunk, count = chunk_plan(size)
        credit = cfg.get("publishing.credit_template").format(streamer=clip["streamer"])
        body = {
            "post_info": {"title": caption_for(clip, credit, 2200), "privacy_level": privacy,
                          "disable_duet": bool(info.get("duet_disabled")),
                          "disable_stitch": bool(info.get("stitch_disabled")),
                          "disable_comment": bool(info.get("comment_disabled")),
                          "video_cover_timestamp_ms": 1000},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                            "total_chunk_count": count},
        }
        d = self._check(httpx.post(f"{API}/post/publish/video/init/", headers=h, json=body, timeout=60), "init")
        publish_id, upload_url = d["publish_id"], d["upload_url"]
        with video.open("rb") as f:
            for i in range(count):
                first = i * chunk
                last = size - 1 if i == count - 1 else first + chunk - 1
                f.seek(first)
                data = f.read(last - first + 1)
                r = httpx.put(upload_url, content=data, timeout=600, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {first}-{last}/{size}"})
                if r.status_code not in (200, 201, 206):
                    raise PublishError(f"TikTok chunk {i + 1}/{count} upload failed: {r.status_code} {r.text[:200]}")
        for _ in range(40):
            wait(6)
            st = self._check(httpx.post(f"{API}/post/publish/status/fetch/", headers=h,
                                        json={"publish_id": publish_id}, timeout=30), "status")
            s = st.get("status")
            if s == "PUBLISH_COMPLETE":
                ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
                return publish_id, (f"https://www.tiktok.com/video/{ids[0]}" if ids else None)
            if s == "FAILED":
                raise PublishError(f"TikTok processing failed: {st.get('fail_reason')}")
        return publish_id, None  # still processing; TikTok will finish it
