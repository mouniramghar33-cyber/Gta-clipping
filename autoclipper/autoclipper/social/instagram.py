"""Instagram Reels via the Instagram Graph API (Instagram Login).

Connect by entering the Instagram professional account's user id and a long-lived access
token with instagram_business_content_publish. Instagram downloads the video from
PUBLIC_BASE_URL, so the server must be reachable over HTTPS.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import AuthError, Permanent, Provider, Result, caption, http, raise_for


class Instagram(Provider):
    name = "instagram"
    needs_public_url = True

    def base(self) -> str:
        host = os.environ.get("IG_GRAPH_HOST", "graph.instagram.com")
        ver = os.environ.get("IG_API_VERSION", "v27.0")
        return f"https://{host}/{ver}"

    def configured(self) -> bool:
        return True  # credentials are entered per account

    def setup_hint(self) -> str:
        return "Enter the Instagram user id and a long-lived token with instagram_business_content_publish."

    def connect(self, user_id: str = "", token: str = "", **kw):
        if not user_id or not token:
            raise Permanent("Instagram user id and token are required")
        r = http("GET", f"{self.base()}/{user_id}", params={"fields": "id,username,name,profile_picture_url",
                                                            "access_token": token})
        if r.status_code in (400, 401, 403):
            raise AuthError(f"Instagram rejected the token: {r.text[:200]}")
        raise_for(r, "Instagram profile")
        d = r.json()
        return [{"external_id": d["id"], "username": d.get("username"), "display_name": d.get("name"),
                 "avatar_url": d.get("profile_picture_url"), "platform": "instagram",
                 "tokens": {"access_token": token}}]

    def verify(self, account):
        tok = self.ctx.tokens(account).get("access_token")
        r = http("GET", f"{self.base()}/{account['external_id']}",
                 params={"fields": "username,name,profile_picture_url", "access_token": tok})
        if r.status_code in (400, 401, 403):
            raise AuthError("Instagram token expired or revoked; reconnect")
        raise_for(r, "Instagram profile")
        d = r.json()
        return {"username": d.get("username"), "display_name": d.get("name"), "avatar_url": d.get("profile_picture_url")}

    def publish(self, job, account, video: Path, public_url, progress):
        st = dict(job.get("upload_state") or {})
        if st.get("container"):
            return Result("processing", st["container"], None, st)
        if not public_url:
            raise Permanent("Instagram needs PUBLIC_BASE_URL (https) to download the video")
        tok = self.ctx.tokens(account).get("access_token")
        r = http("POST", f"{self.base()}/{account['external_id']}/media", data={
            "media_type": "REELS", "video_url": public_url, "caption": caption(job, 2200),
            "share_to_feed": "true", "access_token": tok})
        raise_for(r, "Instagram container")
        st = {"container": r.json()["id"]}
        progress(st)
        return Result("processing", st["container"], None, st)

    def check(self, job, account):
        st = job.get("upload_state") or {}
        tok = self.ctx.tokens(account).get("access_token")
        if st.get("media_id"):
            return Result("published", st["media_id"], job.get("url"))
        r = http("GET", f"{self.base()}/{st['container']}", params={"fields": "status_code,status", "access_token": tok})
        raise_for(r, "Instagram container status")
        code = r.json().get("status_code")
        if code in ("ERROR", "EXPIRED"):
            return Result("failed", st["container"], None, message=f"Instagram {code}: {r.json().get('status')}")
        if code != "FINISHED":
            return Result("processing", st["container"], None, message=code)
        p = http("POST", f"{self.base()}/{account['external_id']}/media_publish",
                 data={"creation_id": st["container"], "access_token": tok})
        raise_for(p, "Instagram publish")
        mid = p.json()["id"]
        link = http("GET", f"{self.base()}/{mid}", params={"fields": "permalink", "access_token": tok}).json().get("permalink")
        return Result("published", mid, link, {**st, "media_id": mid})

    def metrics(self, account, jobs):
        tok = self.ctx.tokens(account).get("access_token")
        out = {}
        for j in jobs:
            mid = (j.get("upload_state") or {}).get("media_id")
            if not mid:
                continue
            r = http("GET", f"{self.base()}/{mid}", params={"fields": "like_count,comments_count", "access_token": tok})
            if r.status_code < 400:
                d = r.json()
                out[j["id"]] = {"likes": d.get("like_count"), "comments": d.get("comments_count")}
        return out
