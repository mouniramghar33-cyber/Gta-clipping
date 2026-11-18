"""Social media publishing providers (official / authorized APIs only).

    youtube     YouTube Data API v3 (OAuth 2.0), Shorts upload, status, statistics + Analytics
    tiktok      TikTok Content Posting API, Direct Post (OAuth 2.0). TikTok requires the user to
                review and consent to every post, so each TikTok job goes through the
                compliant approval screen in the dashboard.
    buffer      Buffer public API (API key; free plan includes API access). Buffer is an
                authorized TikTok/YouTube/Instagram publishing partner and posts unattended.
    uploadpost  Upload-Post API (API key; TikTok requires a paid plan).
    instagram   Instagram Graph API Reels (long-lived token entered in the setup wizard).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx


class ProviderError(Exception):
    transient = False
    auth = False

    def __init__(self, msg: str, retry_at: float | None = None):
        super().__init__(msg)
        self.retry_at = retry_at


class Transient(ProviderError):
    """Temporary problem (network, 5xx, rate limit, quota) - retry later."""
    transient = True


class AuthError(ProviderError):
    """Token expired/revoked or credentials missing - the account must be reconnected."""
    auth = True


class Permanent(ProviderError):
    """The platform rejected this post; retrying the same request won't help."""


@dataclass
class Result:
    state: str                         # processing | published | failed
    remote_id: str | None = None
    url: str | None = None
    upload_state: dict = field(default_factory=dict)
    message: str | None = None


def raise_for(r: httpx.Response, what: str) -> None:
    """Map an HTTP error onto the retry policy."""
    if r.status_code < 400:
        return
    body = r.text[:400]
    if r.status_code in (401,):
        raise AuthError(f"{what}: unauthorized ({body})")
    if r.status_code == 429 or r.status_code >= 500:
        retry = None
        ra = r.headers.get("retry-after")
        if ra and ra.isdigit():
            retry = time.time() + int(ra)
        raise Transient(f"{what}: HTTP {r.status_code} {body}", retry_at=retry)
    if r.status_code == 403 and ("quota" in body.lower() or "rate" in body.lower()):
        raise Transient(f"{what}: quota/rate limit {body}")
    if r.status_code == 403:
        raise AuthError(f"{what}: forbidden ({body})")
    raise Permanent(f"{what}: HTTP {r.status_code} {body}")


def http(method: str, url: str, **kw) -> httpx.Response:
    """HTTP call where network failures become Transient errors."""
    kw.setdefault("timeout", 60)
    try:
        return httpx.request(method, url, **kw)
    except httpx.TransportError as e:
        raise Transient(f"network error calling {url.split('?')[0]}: {e}") from e


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class Context:
    """What providers need: config, DB, encrypted vault."""

    def __init__(self, cfg, db, vault):
        self.cfg, self.db, self.vault = cfg, db, vault

    def tokens(self, account: dict) -> dict:
        return self.vault.decrypt(account.get("tokens_enc"))

    def save_tokens(self, account: dict, tokens: dict, **cols) -> None:
        data = {"tokens_enc": self.vault.encrypt(tokens), "updated_at": time.time(), **cols}
        self.db.update("accounts", account["id"], data)
        account.update(data)


class Provider:
    name = "base"
    oauth = False
    requires_approval = False      # every post needs a human "post" tap (TikTok Direct Post)
    needs_public_url = False       # the platform downloads the video from our server

    def __init__(self, ctx: Context):
        self.ctx = ctx

    def configured(self) -> bool:
        raise NotImplementedError

    def setup_hint(self) -> str:
        return ""

    def oauth_start(self, redirect_uri: str, state: str) -> tuple[str, str | None]:
        raise NotImplementedError

    def oauth_finish(self, code: str, redirect_uri: str, verifier: str | None) -> list[dict]:
        raise NotImplementedError

    def connect(self, **kw) -> list[dict]:
        raise NotImplementedError

    def verify(self, account: dict) -> dict:
        """Refresh profile/token; returns column updates. Raises AuthError if disconnected."""
        raise NotImplementedError

    def publish(self, job: dict, account: dict, video: Path, public_url: str | None, progress) -> Result:
        raise NotImplementedError

    def check(self, job: dict, account: dict) -> Result:
        raise NotImplementedError

    def metrics(self, account: dict, jobs: list[dict]) -> dict[int, dict]:
        return {}


def caption(job: dict, max_len: int) -> str:
    tags = " ".join(f"#{t}" for t in (job.get("hashtags") or []) if t)
    text = (job.get("description") or "").strip()
    if tags and tags not in text:
        text = f"{text}\n{tags}" if text else tags
    return text[:max_len]


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)
