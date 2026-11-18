"""Upload-Post API (https://api.upload-post.com, header `Authorization: Apikey <key>`).

Paid plans start at $16/month (annual) / $24 (monthly); TikTok is not available on the free
plan (the API returns 403). Each Upload-Post profile + connected platform = one account here.
The video file is uploaded directly (no public URL needed).
"""
from __future__ import annotations

from pathlib import Path

from . import AuthError, Permanent, Provider, Result, caption, http, raise_for

API = "https://api.upload-post.com/api"


class UploadPost(Provider):
    name = "uploadpost"

    def key(self):
        return self.ctx.vault.get("UPLOADPOST_API_KEY")

    def configured(self) -> bool:
        return bool(self.key())

    def setup_hint(self) -> str:
        return "Create an API key in the Upload-Post dashboard and save it as UPLOADPOST_API_KEY (TikTok needs a paid plan)."

    def _h(self):
        k = self.key()
        if not k:
            raise AuthError("UPLOADPOST_API_KEY is not set")
        return {"Authorization": f"Apikey {k}"}

    def connect(self, **kw):
        r = http("GET", f"{API}/uploadposts/users", headers=self._h())
        raise_for(r, "Upload-Post users")
        out = []
        for p in r.json().get("profiles", []):
            for platform, acc in (p.get("social_accounts") or {}).items():
                if not acc or not isinstance(acc, dict):
                    continue
                out.append({"external_id": f"{p['username']}:{platform}", "platform": platform,
                            "username": acc.get("handle") or acc.get("username"),
                            "display_name": acc.get("display_name"), "avatar_url": acc.get("social_images"),
                            "settings": {"user": p["username"], "platform": platform}, "tokens": {}})
        return out

    def verify(self, account):
        for a in self.connect():
            if a["external_id"] == account["external_id"]:
                return {"display_name": a["display_name"], "avatar_url": a["avatar_url"]}
        raise AuthError("profile/platform no longer connected in Upload-Post")

    def publish(self, job, account, video: Path, public_url, progress):
        st = dict(job.get("upload_state") or {})
        if st.get("request_id"):
            return Result("processing", st["request_id"], None, st)
        s = account.get("settings") or {}
        platform = s["platform"]
        req_id = f"autoclipper-job-{job['id']}"
        data = {"user": s["user"], "platform[]": platform, "title": (job.get("title") or "")[:100],
                "description": caption(job, 2200), "async_upload": "true", "external_id": f"job-{job['id']}"}
        opts = job.get("options") or {}
        if platform == "tiktok":
            data.update({"privacy_level": opts.get("privacy_level") or account.get("privacy") or "PUBLIC_TO_EVERYONE",
                         "is_aigc": str(bool(account.get("ai_disclosure"))).lower()})
        if platform == "youtube":
            data.update({"privacyStatus": account.get("privacy") or "public",
                         "containsSyntheticMedia": str(bool(account.get("ai_disclosure"))).lower(),
                         "selfDeclaredMadeForKids": "false"})
        st = {"request_id": req_id}
        progress(st)  # our own request id: if the response is lost we can still poll it
        with video.open("rb") as f:
            r = http("POST", f"{API}/upload", headers={**self._h(), "X-Request-Id": req_id}, data=data,
                     files={"video": (video.name, f, "video/mp4")}, timeout=900)
        if r.status_code == 403:
            raise Permanent(f"Upload-Post refused (plan restriction?): {r.text[:200]}")
        raise_for(r, "Upload-Post upload")
        return Result("processing", req_id, None, st)

    def check(self, job, account):
        rid = (job.get("upload_state") or {}).get("request_id") or job["remote_id"]
        r = http("GET", f"{API}/uploadposts/status", params={"request_id": rid}, headers=self._h())
        if r.status_code == 404:
            return Result("processing", rid, None, message="not found yet")
        raise_for(r, "Upload-Post status")
        d = r.json()
        res = (d.get("results") or [{}])[0]
        if d.get("status") == "completed" or res.get("status") == "completed":
            return Result("published", res.get("platform_post_id") or rid, res.get("post_url"),
                          message=res.get("message"))
        if d.get("status") == "failed" or res.get("status") in ("failed", "skipped"):
            return Result("failed", rid, None, message=res.get("message") or d.get("message"))
        return Result("processing", rid, None, message=d.get("status"))
