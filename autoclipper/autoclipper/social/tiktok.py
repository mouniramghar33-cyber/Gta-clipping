"""TikTok via the official Content Posting API (Direct Post) with Login Kit OAuth 2.0.

TikTok's Content Sharing Guidelines require, for every post:
  * the creator's nickname is shown (fresh creator_info),
  * the user picks the privacy level manually (no default),
  * comment/duet/stitch are off by default and turned on manually (disabled if the creator
    disabled them),
  * commercial content disclosure, a consent declaration and a preview,
  * content is only sent after the user explicitly consents.
So TikTok jobs always wait in the dashboard's approval screen. One tap posts after review.
Unaudited apps may only post SELF_ONLY, for at most 5 users per 24 h, and posts are capped at
around 15 per creator per day.
"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlencode

from . import AuthError, Permanent, Provider, Result, Transient, http, raise_for

API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
MB = 1024 * 1024
PRIVACY = {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}


def chunk_plan(size: int, chunk: int = 10 * MB) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok's media transfer rules (5-64 MB chunks)."""
    if size < 5 * MB:
        return size, 1
    return chunk, max(1, size // chunk)


def validate_approval(opts: dict, creator: dict, duration: float | None) -> list[str]:
    """Check a user's approval form against TikTok's rules. Returns a list of problems."""
    errors = []
    allowed = creator.get("privacy_level_options") or []
    if opts.get("privacy_level") not in allowed:
        errors.append("choose who can view this video")
    for k, disabled_key in (("allow_comment", "comment_disabled"), ("allow_duet", "duet_disabled"),
                            ("allow_stitch", "stitch_disabled")):
        if opts.get(k) and creator.get(disabled_key):
            errors.append(f"{k.replace('allow_', '')} is disabled in the creator's TikTok settings")
    if opts.get("commercial") and not (opts.get("brand_organic") or opts.get("brand_content")):
        errors.append("select 'Your brand' and/or 'Branded content' or turn off content disclosure")
    if opts.get("brand_content") and opts.get("privacy_level") == "SELF_ONLY":
        errors.append("branded content cannot be private")
    if not opts.get("consent"):
        errors.append("confirm the declaration")
    mx = creator.get("max_video_post_duration_sec")
    if duration and mx and duration > mx:
        errors.append(f"video is longer than this creator's limit ({mx}s)")
    if not (opts.get("title") or "").strip():
        errors.append("add a caption")
    return errors


class TikTok(Provider):
    name = "tiktok"
    oauth = True
    requires_approval = True

    def _cred(self):
        v = self.ctx.vault
        return v.get("TIKTOK_CLIENT_KEY"), v.get("TIKTOK_CLIENT_SECRET")

    def configured(self) -> bool:
        return all(self._cred())

    def setup_hint(self) -> str:
        return "Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET (TikTok for Developers app with Login Kit + Content Posting API)."

    def scopes(self) -> str:
        return self.ctx.cfg.get("publishing.tiktok_scopes") or "user.info.basic,video.publish,video.list"

    def oauth_start(self, redirect_uri, state):
        key, _ = self._cred()
        q = {"client_key": key, "response_type": "code", "scope": self.scopes(),
             "redirect_uri": redirect_uri, "state": state, "disable_auto_auth": 1}
        return f"{AUTH_URL}?{urlencode(q)}", None

    def oauth_finish(self, code, redirect_uri, verifier):
        key, sec = self._cred()
        r = http("POST", f"{API}/oauth/token/", data={
            "client_key": key, "client_secret": sec, "code": code, "grant_type": "authorization_code",
            "redirect_uri": redirect_uri}, headers={"Content-Type": "application/x-www-form-urlencoded"})
        raise_for(r, "TikTok token exchange")
        t = r.json()
        if "access_token" not in t:
            raise AuthError(f"TikTok token exchange failed: {t.get('error_description') or t}")
        tokens = self._tok(t)
        user = self._user(tokens["access_token"])
        return [{"external_id": t.get("open_id") or user.get("open_id"), "username": user.get("username"),
                 "display_name": user.get("display_name"), "avatar_url": user.get("avatar_url"),
                 "platform": "tiktok", "tokens": tokens, "token_expires_at": tokens["expires_at"],
                 "refresh_expires_at": tokens.get("refresh_expires_at")}]

    @staticmethod
    def _tok(t: dict) -> dict:
        now = time.time()
        return {"access_token": t["access_token"], "refresh_token": t.get("refresh_token"),
                "scope": t.get("scope", ""), "open_id": t.get("open_id"),
                "expires_at": now + int(t.get("expires_in", 86400)),
                "refresh_expires_at": now + int(t.get("refresh_expires_in", 0)) if t.get("refresh_expires_in") else None}

    def _user(self, access: str) -> dict:
        fields = "open_id,avatar_url,display_name"
        if "user.info.profile" in self.scopes():
            fields += ",username"
        r = http("GET", f"{API}/user/info/", params={"fields": fields},
                 headers={"Authorization": f"Bearer {access}"})
        d = self._check(r, "user info")
        return d.get("user", d)

    @staticmethod
    def _check(r, what: str) -> dict:
        try:
            d = r.json()
        except ValueError:
            raise_for(r, f"TikTok {what}")
            raise Transient(f"TikTok {what}: HTTP {r.status_code}")
        code = (d.get("error") or {}).get("code")
        if code in (None, "ok"):
            if r.status_code >= 400:
                raise_for(r, f"TikTok {what}")
            return d.get("data") or {}
        msg = f"TikTok {what}: {code} {(d.get('error') or {}).get('message', '')}"
        if code in ("access_token_invalid", "scope_not_authorized", "invalid_token"):
            raise AuthError(msg)
        if code in ("rate_limit_exceeded", "spam_risk_too_many_posts", "reached_active_user_cap") or r.status_code >= 500:
            raise Transient(msg, retry_at=time.time() + (3600 if "spam" in code or "cap" in code else 120))
        raise Permanent(msg)

    def access_token(self, account: dict) -> str:
        tokens = self.ctx.tokens(account)
        if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 300:
            return tokens["access_token"]
        key, sec = self._cred()
        if not tokens.get("refresh_token") or not (key and sec):
            raise AuthError("TikTok authorization expired; reconnect the account")
        r = http("POST", f"{API}/oauth/token/", data={
            "client_key": key, "client_secret": sec, "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"]}, headers={"Content-Type": "application/x-www-form-urlencoded"})
        t = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if "access_token" not in t:
            if r.status_code >= 500:
                raise Transient(f"TikTok token refresh: HTTP {r.status_code}")
            raise AuthError(f"TikTok authorization expired or revoked ({t.get('error') or r.status_code}); reconnect")
        new = self._tok(t)  # TikTok rotates refresh tokens: always store the new one
        tokens.update({k: v for k, v in new.items() if v is not None})
        self.ctx.save_tokens(account, tokens, token_expires_at=tokens["expires_at"],
                             refresh_expires_at=tokens.get("refresh_expires_at"), status="connected",
                             status_message=None)
        return tokens["access_token"]

    def creator_info(self, account: dict) -> dict:
        token = self.access_token(account)
        r = http("POST", f"{API}/post/publish/creator_info/query/",
                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
        return self._check(r, "creator_info")

    def verify(self, account):
        u = self._user(self.access_token(account))
        return {"display_name": u.get("display_name"), "avatar_url": u.get("avatar_url"),
                **({"username": u["username"]} if u.get("username") else {})}

    def publish(self, job, account, video: Path, public_url, progress):
        opts = job.get("options") or {}
        if not job.get("approved_at") or not opts.get("consent"):
            raise Permanent("TikTok posts need the user's approval first")
        st = dict(job.get("upload_state") or {})
        if st.get("publish_id") and st.get("uploaded"):
            return Result("processing", st["publish_id"], None, st)  # already sent: just track it
        token = self.access_token(account)
        creator = self.creator_info(account)  # also enforces "can post right now"
        problems = validate_approval(opts, creator, job.get("duration"))
        if problems:
            raise Permanent("approval no longer valid: " + "; ".join(problems))
        size = video.stat().st_size
        chunk, count = chunk_plan(size)
        post_info = {"title": opts["title"][:2200], "privacy_level": opts["privacy_level"],
                     "disable_comment": not opts.get("allow_comment"),
                     "disable_duet": not opts.get("allow_duet"),
                     "disable_stitch": not opts.get("allow_stitch"),
                     "video_cover_timestamp_ms": int(opts.get("cover_ms", 1000)),
                     "brand_content_toggle": bool(opts.get("brand_content")),
                     "brand_organic_toggle": bool(opts.get("brand_organic")),
                     "is_aigc": bool(account.get("ai_disclosure"))}
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        r = http("POST", f"{API}/post/publish/video/init/", headers=h, json={
            "post_info": post_info,
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                            "total_chunk_count": count}})
        d = self._check(r, "init")
        st = {"publish_id": d["publish_id"], "uploaded": False}
        progress(st)
        with video.open("rb") as f:
            for i in range(count):
                first = i * chunk
                last = size - 1 if i == count - 1 else first + chunk - 1
                f.seek(first)
                data = f.read(last - first + 1)
                r = http("PUT", d["upload_url"], content=data, timeout=600, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {first}-{last}/{size}"})
                if r.status_code not in (200, 201, 206):
                    raise_for(r, f"TikTok chunk {i + 1}/{count}")
        st["uploaded"] = True
        progress(st)
        return Result("processing", st["publish_id"], None, st)

    def check(self, job, account):
        token = self.access_token(account)
        pid = (job.get("upload_state") or {}).get("publish_id") or job["remote_id"]
        r = http("POST", f"{API}/post/publish/status/fetch/", json={"publish_id": pid},
                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
        d = self._check(r, "status")
        s = d.get("status")
        if s == "PUBLISH_COMPLETE":
            ids = d.get("publicaly_available_post_id") or d.get("publicly_available_post_id") or []
            url = None
            if ids:
                user = account.get("username")
                url = f"https://www.tiktok.com/@{user}/video/{ids[0]}" if user else f"https://www.tiktok.com/video/{ids[0]}"
            return Result("published", str(ids[0]) if ids else pid, url,
                          {**(job.get("upload_state") or {}), "post_id": str(ids[0]) if ids else None},
                          message="private (SELF_ONLY) posts have no public URL" if not ids else None)
        if s == "FAILED":
            return Result("failed", pid, None, message=f"TikTok: {d.get('fail_reason')}")
        return Result("processing", pid, None, message=s)

    def metrics(self, account, jobs):
        if "video.list" not in self.ctx.tokens(account).get("scope", ""):
            return {}
        by = {(j.get("upload_state") or {}).get("post_id"): j for j in jobs
              if (j.get("upload_state") or {}).get("post_id")}
        ids = list(by)
        out = {}
        token = self.access_token(account)
        for i in range(0, len(ids), 20):
            r = http("POST", f"{API}/video/query/", params={"fields": "id,view_count,like_count,comment_count,share_count"},
                     json={"filters": {"video_ids": ids[i:i + 20]}},
                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            d = self._check(r, "video query")
            for v in d.get("videos", []):
                j = by.get(str(v.get("id")))
                if j:
                    out[j["id"]] = {"views": v.get("view_count"), "likes": v.get("like_count"),
                                    "comments": v.get("comment_count"), "shares": v.get("share_count")}
        return out
