"""Buffer public API (GraphQL at https://api.buffer.com, personal API key).

The free Buffer plan includes API access (1 key, 3 channels). Each Buffer channel (TikTok,
YouTube, Instagram, ...) becomes one account here. Buffer publishes through its own
authorized platform integrations, so posts go out unattended (schedulingType: automatic).
Buffer downloads the video from a URL, so PUBLIC_BASE_URL must be reachable over HTTPS.
Per Buffer's help center, TikTok channels must be TikTok Business accounts for automatic
publishing.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import AuthError, Permanent, Provider, Result, Transient, caption, http, raise_for

API = "https://api.buffer.com"


def lit(v: str) -> str:
    """GraphQL string literal (JSON string escaping is valid GraphQL string syntax)."""
    return json.dumps(str(v))


class Buffer(Provider):
    name = "buffer"
    needs_public_url = True

    def key(self) -> str | None:
        return self.ctx.vault.get("BUFFER_API_KEY")

    def configured(self) -> bool:
        return bool(self.key())

    def setup_hint(self) -> str:
        return "Create a personal API key at publish.buffer.com/settings/api and save it as BUFFER_API_KEY."

    def gql(self, query: str, variables: dict | None = None) -> dict:
        key = self.key()
        if not key:
            raise AuthError("BUFFER_API_KEY is not set")
        r = http("POST", API, json={"query": query, "variables": variables or {}},
                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        raise_for(r, "Buffer API")
        d = r.json()
        if d.get("errors"):
            msg = "; ".join(e.get("message", "") for e in d["errors"])
            low = msg.lower()
            if "unauth" in low or "forbidden" in low or "api key" in low:
                raise AuthError(f"Buffer: {msg}")
            if "rate" in low or "limit" in low:
                raise Transient(f"Buffer: {msg}", retry_at=time.time() + 900)
            raise Permanent(f"Buffer: {msg}")
        return d.get("data") or {}

    def connect(self, **kw) -> list[dict]:
        orgs = self.gql("query { account { organizations { id name } } }")["account"]["organizations"]
        out = []
        for o in orgs:
            q = (f'query {{ channels(input: {{ organizationId: {lit(o["id"])} }}) '
                 '{ id name displayName service avatar isQueuePaused } }')
            for ch in self.gql(q).get("channels", []):
                out.append({"external_id": ch["id"], "username": ch.get("name"),
                            "display_name": ch.get("displayName") or ch.get("name"),
                            "avatar_url": ch.get("avatar"), "platform": str(ch.get("service", "")).lower(),
                            "settings": {"organization_id": o["id"], "service": ch.get("service")},
                            "tokens": {}})
        return out

    def verify(self, account):
        org = (account.get("settings") or {}).get("organization_id")
        q = (f'query {{ channels(input: {{ organizationId: {lit(org)} }}) '
             '{ id name displayName avatar isQueuePaused } }')
        for ch in self.gql(q).get("channels", []):
            if ch["id"] == account["external_id"]:
                return {"display_name": ch.get("displayName") or ch.get("name"), "avatar_url": ch.get("avatar"),
                        "status_message": "Buffer queue is paused" if ch.get("isQueuePaused") else None}
        raise AuthError("channel no longer exists in Buffer")

    def publish(self, job, account, video: Path, public_url, progress):
        st = dict(job.get("upload_state") or {})
        if st.get("post_id"):
            return Result("processing", st["post_id"], None, st)  # created before a crash: don't duplicate
        if not public_url:
            raise Permanent("Buffer needs PUBLIC_BASE_URL (https) to download the video")
        text = caption(job, 2200)
        ai = "true" if account.get("ai_disclosure") else "false"
        platform = account.get("platform")
        if platform == "youtube":
            priv = account.get("privacy") or self.ctx.cfg.get("publishing.youtube_privacy") or "public"
            meta = (f"metadata: {{ youtube: {{ title: {lit((job.get('title') or '')[:100])}, categoryId: "
                    f"{lit(self.ctx.cfg.get('publishing.youtube_category') or '20')}, privacy: {priv}, "
                    f"madeForKids: false, isAiGenerated: {ai} }} }}, ")
        elif platform == "tiktok":
            meta = f"metadata: {{ tiktok: {{ isAiGenerated: {ai} }} }}, "
        else:
            meta = ""
        due = datetime.fromtimestamp(time.time() + 120, timezone.utc).isoformat().replace("+00:00", "Z")
        m = (f'mutation {{ createPost(input: {{ text: {lit(text)}, channelId: {lit(account["external_id"])}, '
             f'schedulingType: automatic, mode: customScheduled, dueAt: {lit(due)}, {meta}'
             f'assets: [{{ video: {{ url: {lit(public_url)}, metadata: {{ thumbnailOffset: 1000 }} }} }}] }}) {{ '
             '... on PostActionSuccess { post { id } } ... on MutationError { message } } }')
        d = self.gql(m)
        res = d.get("createPost") or {}
        if res.get("message"):
            raise Permanent(f"Buffer: {res['message']}")
        pid = res["post"]["id"]
        st = {"post_id": pid, "due_at": due}
        progress(st)
        return Result("processing", pid, None, st)

    def check(self, job, account):
        pid = (job.get("upload_state") or {}).get("post_id") or job["remote_id"]
        d = self.gql(f"query {{ post(input: {{ id: {lit(pid)} }}) {{ id status externalLink error {{ message }} }} }}")
        post = d.get("post") or {}
        status = str(post.get("status", "")).lower()
        if status == "sent":
            return Result("published", pid, post.get("externalLink"), message="sent by Buffer")
        if status == "error":
            return Result("failed", pid, None, message=f"Buffer: {(post.get('error') or {}).get('message') or 'publishing error'}")
        if status == "needs_approval":
            return Result("processing", pid, None, message="waiting for approval inside Buffer")
        return Result("processing", pid, None, message=status or "scheduled")

    def metrics(self, account, jobs):
        out = {}
        for j in jobs:
            pid = (j.get("upload_state") or {}).get("post_id")
            if not pid:
                continue
            d = self.gql(f"query {{ post(input: {{ id: {lit(pid)} }}) {{ metrics {{ type name value }} }} }}")
            vals = {}
            for mt in (d.get("post") or {}).get("metrics") or []:
                key = f"{mt.get('type', '')} {mt.get('name', '')}".lower()
                for field, words in (("views", ("view", "impression", "play")), ("likes", ("like", "reaction")),
                                     ("comments", ("comment",)), ("shares", ("share", "repost"))):
                    if any(w in key for w in words) and field not in vals:
                        try:
                            vals[field] = int(float(mt.get("value")))
                        except (TypeError, ValueError):
                            pass
            if vals:
                out[j["id"]] = vals
        return out
