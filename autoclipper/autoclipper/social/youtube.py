"""YouTube Shorts via the official YouTube Data API v3.

Setup: Google Cloud project -> enable "YouTube Data API v3" (+ "YouTube Analytics API" for
watch time/retention) -> OAuth client of type "Web application" with the redirect URI shown
in the setup wizard. Credentials: YT_CLIENT_ID / YT_CLIENT_SECRET.
Note: while the OAuth consent screen is in "Testing" mode Google expires refresh tokens after
7 days. Publish the consent screen (In production) for unattended long-term operation.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import AuthError, Permanent, Provider, Result, Transient, caption, http, pkce_pair, raise_for

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]
PT = ZoneInfo("America/Los_Angeles")  # YouTube quotas reset at midnight Pacific time


def build_body(job: dict, account: dict, cfg) -> dict:
    tags = [t for t in (job.get("hashtags") or []) if t][:15]
    desc = caption(job, 4800)
    if "#shorts" not in desc.lower():
        desc += " #Shorts"
    opts = job.get("options") or {}
    return {
        "snippet": {"title": (job.get("title") or "")[:100], "description": desc, "tags": tags,
                    "categoryId": str(opts.get("category") or cfg.get("publishing.youtube_category") or "20")},
        "status": {"privacyStatus": opts.get("privacy") or account.get("privacy")
                   or cfg.get("publishing.youtube_privacy") or "public",
                   "selfDeclaredMadeForKids": False,
                   "containsSyntheticMedia": bool(account.get("ai_disclosure"))},
    }


class YouTube(Provider):
    name = "youtube"
    oauth = True

    def _cred(self):
        v = self.ctx.vault
        return v.get("YT_CLIENT_ID"), v.get("YT_CLIENT_SECRET")

    def configured(self) -> bool:
        return all(self._cred())

    def setup_hint(self) -> str:
        return "Set YT_CLIENT_ID and YT_CLIENT_SECRET (Google Cloud OAuth client, type Web application)."

    # ---- OAuth -------------------------------------------------------------
    def oauth_start(self, redirect_uri, state):
        cid, _ = self._cred()
        verifier, challenge = pkce_pair()
        q = {"client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent select_account",
             "include_granted_scopes": "true", "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{AUTH_URL}?{urlencode(q)}", verifier

    def oauth_finish(self, code, redirect_uri, verifier):
        cid, sec = self._cred()
        r = http("POST", TOKEN_URL, data={"code": code, "client_id": cid, "client_secret": sec,
                                          "redirect_uri": redirect_uri, "grant_type": "authorization_code",
                                          "code_verifier": verifier})
        raise_for(r, "YouTube token exchange")
        t = r.json()
        if not t.get("refresh_token"):
            raise AuthError("Google returned no refresh token; remove the app's access in your Google "
                            "account settings and connect again")
        tokens = {"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                  "scope": t.get("scope", ""), "expires_at": time.time() + t.get("expires_in", 3600)}
        ch = self._channel(tokens["access_token"])
        return [{"external_id": ch["id"], "username": ch["snippet"].get("customUrl"),
                 "display_name": ch["snippet"]["title"],
                 "avatar_url": ch["snippet"].get("thumbnails", {}).get("default", {}).get("url"),
                 "platform": "youtube", "tokens": tokens, "token_expires_at": tokens["expires_at"]}]

    def _channel(self, access: str) -> dict:
        r = http("GET", f"{API}/channels", params={"part": "snippet,statistics", "mine": "true"},
                 headers={"Authorization": f"Bearer {access}"})
        raise_for(r, "YouTube channels.list")
        items = r.json().get("items") or []
        if not items:
            raise Permanent("this Google account has no YouTube channel")
        return items[0]

    def access_token(self, account: dict) -> str:
        tokens = self.ctx.tokens(account)
        if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 120:
            return tokens["access_token"]
        cid, sec = self._cred()
        if not (cid and sec):
            raise AuthError("YouTube client credentials are not configured")
        r = http("POST", TOKEN_URL, data={"client_id": cid, "client_secret": sec,
                                          "refresh_token": tokens.get("refresh_token"),
                                          "grant_type": "refresh_token"})
        if r.status_code in (400, 401):
            err = r.json().get("error", "") if r.headers.get("content-type", "").startswith("application/json") else ""
            raise AuthError(f"YouTube authorization expired or revoked ({err or r.status_code}); reconnect the account")
        raise_for(r, "YouTube token refresh")
        t = r.json()
        tokens.update(access_token=t["access_token"], expires_at=time.time() + t.get("expires_in", 3600))
        self.ctx.save_tokens(account, tokens, token_expires_at=tokens["expires_at"], status="connected",
                             status_message=None)
        return tokens["access_token"]

    def verify(self, account):
        ch = self._channel(self.access_token(account))
        return {"display_name": ch["snippet"]["title"], "username": ch["snippet"].get("customUrl"),
                "avatar_url": ch["snippet"].get("thumbnails", {}).get("default", {}).get("url")}

    # ---- quota -------------------------------------------------------------
    def _quota_key(self) -> str:
        return "yt_uploads:" + datetime.now(PT).strftime("%Y-%m-%d")

    def _next_reset(self) -> float:
        now = datetime.now(PT)
        return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), PT).timestamp() + 60

    def quota_used(self) -> int:
        return int(self.ctx.db.kv_get(self._quota_key(), 0))

    # ---- publish -----------------------------------------------------------
    def publish(self, job, account, video: Path, public_url, progress):
        limit = int(self.ctx.cfg.get("publishing.youtube_daily_uploads") or 100)
        st = dict(job.get("upload_state") or {})
        if not st.get("session") and self.quota_used() >= limit:
            raise Transient("YouTube daily upload quota reached for this API project", retry_at=self._next_reset())
        token = self.access_token(account)
        size = video.stat().st_size
        session = st.get("session")
        if not session:
            r = http("POST", UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                              "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
                     json=build_body(job, account, self.ctx.cfg))
            if r.status_code == 403 and ("quotaExceeded" in r.text or "uploadLimitExceeded" in r.text):
                raise Transient("YouTube quota/upload limit exceeded", retry_at=self._next_reset())
            raise_for(r, "YouTube upload init")
            session = r.headers.get("location")
            if not session:
                raise Transient("YouTube did not return an upload session")
            st = {"session": session, "session_created": time.time()}
            progress(st)  # persist before sending bytes so a crash can resume
            self.ctx.db.kv_set(self._quota_key(), self.quota_used() + 1)
        offset = self._uploaded_bytes(session, token, size)
        if offset is True:
            return self._done(st, session, token, size)
        chunk = 8 * 1024 * 1024
        with video.open("rb") as f:
            while offset < size:
                f.seek(offset)
                data = f.read(chunk)
                last = offset + len(data) - 1
                r = http("PUT", session, content=data, timeout=600, headers={
                    "Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {offset}-{last}/{size}"})
                if r.status_code in (200, 201):
                    vid = r.json()["id"]
                    return Result("processing", vid, f"https://youtube.com/shorts/{vid}", st)
                if r.status_code == 404:
                    progress({})
                    raise Transient("YouTube upload session expired; restarting upload")
                if r.status_code != 308:
                    raise_for(r, "YouTube upload")
                rng = r.headers.get("range")
                offset = int(rng.split("-")[1]) + 1 if rng else 0
                progress({**st, "uploaded": offset, "size": size})
        return self._done(st, session, token, size)

    def _uploaded_bytes(self, session, token, size):
        r = http("PUT", session, headers={"Authorization": f"Bearer {token}", "Content-Length": "0",
                                          "Content-Range": f"bytes */{size}"})
        if r.status_code in (200, 201):
            return True
        if r.status_code == 404:
            raise Transient("YouTube upload session expired; restarting upload")
        if r.status_code != 308:
            raise_for(r, "YouTube upload status")
        rng = r.headers.get("range")
        return int(rng.split("-")[1]) + 1 if rng else 0

    def _done(self, st, session, token, size):
        r = http("PUT", session, headers={"Authorization": f"Bearer {token}", "Content-Length": "0",
                                          "Content-Range": f"bytes */{size}"})
        raise_for(r, "YouTube upload finish")
        vid = r.json()["id"]
        return Result("processing", vid, f"https://youtube.com/shorts/{vid}", st)

    def check(self, job, account):
        token = self.access_token(account)
        r = http("GET", f"{API}/videos", params={"part": "status,processingDetails", "id": job["remote_id"]},
                 headers={"Authorization": f"Bearer {token}"})
        raise_for(r, "YouTube videos.list")
        items = r.json().get("items") or []
        if not items:
            return Result("processing", job["remote_id"], job.get("url"), message="not visible yet")
        s = items[0]["status"]
        pd = items[0].get("processingDetails", {})
        if s.get("uploadStatus") in ("rejected", "failed", "deleted"):
            reason = s.get("rejectionReason") or s.get("failureReason") or s.get("uploadStatus")
            return Result("failed", job["remote_id"], job.get("url"), message=f"YouTube {s['uploadStatus']}: {reason}")
        if s.get("uploadStatus") == "processed" or pd.get("processingStatus") == "succeeded":
            return Result("published", job["remote_id"], f"https://youtube.com/shorts/{job['remote_id']}",
                          message=f"privacy={s.get('privacyStatus')}")
        return Result("processing", job["remote_id"], job.get("url"), message=pd.get("processingStatus"))

    # ---- analytics -----------------------------------------------------------
    def metrics(self, account, jobs):
        token = self.access_token(account)
        ids = [j["remote_id"] for j in jobs if j.get("remote_id")]
        out: dict[int, dict] = {}
        by_id = {j["remote_id"]: j for j in jobs if j.get("remote_id")}
        for i in range(0, len(ids), 50):
            r = http("GET", f"{API}/videos", params={"part": "statistics", "id": ",".join(ids[i:i + 50])},
                     headers={"Authorization": f"Bearer {token}"})
            raise_for(r, "YouTube statistics")
            for it in r.json().get("items", []):
                st = it.get("statistics", {})
                out[by_id[it["id"]]["id"]] = {
                    "views": _int(st.get("viewCount")), "likes": _int(st.get("likeCount")),
                    "comments": _int(st.get("commentCount")), "shares": None}
        # Watch time / retention / subscribers from the YouTube Analytics API (if authorized).
        tokens = self.ctx.tokens(account)
        if "yt-analytics" in tokens.get("scope", "") and ids:
            start = min(datetime.fromtimestamp(j.get("published_at") or j["created_at"], timezone.utc).date()
                        for j in jobs)
            for i in range(0, len(ids), 200):
                r = http("GET", ANALYTICS, headers={"Authorization": f"Bearer {token}"}, params={
                    "ids": "channel==MINE", "startDate": start.isoformat(), "endDate": date.today().isoformat(),
                    "metrics": "estimatedMinutesWatched,averageViewPercentage,subscribersGained",
                    "dimensions": "video", "filters": "video==" + ",".join(ids[i:i + 200])})
                if r.status_code >= 400:
                    break  # analytics API not enabled/authorized: keep basic stats, invent nothing
                for row in r.json().get("rows", []) or []:
                    vid, minutes, pct, subs = row[0], row[1], row[2], row[3]
                    if vid in by_id:
                        out.setdefault(by_id[vid]["id"], {}).update(
                            watch_time_min=float(minutes), avg_view_pct=float(pct), subs_gained=int(subs))
        return out


def _int(v):
    return int(v) if v is not None and str(v).isdigit() else None
