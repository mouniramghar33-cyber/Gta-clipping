"""Instagram Reels via the Instagram Graph API content publishing flow.

Env: IG_USER_ID, IG_ACCESS_TOKEN (instagram_business_content_publish), optional IG_API_VERSION,
IG_GRAPH_HOST (graph.instagram.com for Instagram Login, graph.facebook.com for Facebook Login).
Instagram pulls the video from a public URL, so PUBLIC_BASE_URL must point at this server.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

from . import Publisher, PublishError, caption_for, wait


class InstagramPublisher(Publisher):
    name = "instagram"

    def configured(self) -> bool:
        return all(os.environ.get(k) for k in ("IG_USER_ID", "IG_ACCESS_TOKEN", "PUBLIC_BASE_URL"))

    def publish(self, clip, video: Path, public_url, cfg):
        if not public_url:
            raise PublishError("Instagram needs PUBLIC_BASE_URL so it can download the video")
        host = os.environ.get("IG_GRAPH_HOST", "graph.instagram.com")
        ver = os.environ.get("IG_API_VERSION", "v27.0")
        base = f"https://{host}/{ver}"
        ig, tok = os.environ["IG_USER_ID"], os.environ["IG_ACCESS_TOKEN"]
        credit = cfg.get("publishing.credit_template").format(streamer=clip["streamer"])
        r = httpx.post(f"{base}/{ig}/media", timeout=60, data={
            "media_type": "REELS", "video_url": public_url, "caption": caption_for(clip, credit, 2200),
            "share_to_feed": "true", "access_token": tok})
        d = r.json()
        if "id" not in d:
            raise PublishError(f"Instagram container failed: {str(d)[:300]}")
        container = d["id"]
        for _ in range(20):
            wait(15)
            st = httpx.get(f"{base}/{container}", params={"fields": "status_code,status", "access_token": tok},
                           timeout=30).json()
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram processing {code}: {st.get('status')}")
        else:
            raise PublishError("Instagram processing timed out")
        p = httpx.post(f"{base}/{ig}/media_publish", data={"creation_id": container, "access_token": tok},
                       timeout=60).json()
        if "id" not in p:
            raise PublishError(f"Instagram publish failed: {str(p)[:300]}")
        link = httpx.get(f"{base}/{p['id']}", params={"fields": "permalink", "access_token": tok},
                         timeout=30).json().get("permalink")
        return p["id"], link
