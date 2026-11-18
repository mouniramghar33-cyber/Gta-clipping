"""Chat for Kick and YouTube Live through their official APIs (Twitch is in chat.py).

Kick: official Public API event subscriptions. AutoClipper subscribes to `chat.message.sent`
  for each Kick channel on the watchlist (app access token, `broadcaster_user_id`). Kick
  delivers messages to the webhook URL configured in your Kick developer app:
  {PUBLIC_BASE_URL}/webhooks/kick. Every delivery is verified with Kick's RSA public key
  (SHA-256, PKCS#1 v1.5 over "message_id.timestamp.body") and de-duplicated by message id.
  Needs KICK_CLIENT_ID / KICK_CLIENT_SECRET and a public HTTPS address.

YouTube: YouTube Data API v3 with an API key. videos.list (1 unit) gives
  liveStreamingDetails.activeLiveChatId, then liveChatMessages.list (5 units per call) is polled
  no faster than YouTube's pollingIntervalMillis and within a daily quota budget.
  Needs YT_API_KEY.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections import OrderedDict
from datetime import datetime
from typing import Callable

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)

KICK_API = "https://api.kick.com/public/v1"
KICK_TOKEN = "https://id.kick.com/oauth/token"


def _ts(iso: str | None) -> float:
    if not iso:
        return time.time()
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


class KickChat:
    def __init__(self, vault, on_message: Callable[[str, float, str, str], None]):
        self.vault = vault
        self.on_message = on_message          # (broadcaster_user_id, ts, user, text)
        self._token: dict = {"v": None, "exp": 0}
        self._pubkey = None
        self._pubkey_at = 0.0
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.subscribed: dict[str, str] = {}  # broadcaster_user_id -> subscription id
        self.last_error: str | None = None
        self.last_message_at = 0.0

    def configured(self) -> bool:
        return bool(self.vault.get("KICK_CLIENT_ID") and self.vault.get("KICK_CLIENT_SECRET"))

    def _app_token(self) -> str:
        if self._token["v"] and time.time() < self._token["exp"] - 120:
            return self._token["v"]
        r = httpx.post(KICK_TOKEN, timeout=30, data={
            "grant_type": "client_credentials", "client_id": self.vault.get("KICK_CLIENT_ID"),
            "client_secret": self.vault.get("KICK_CLIENT_SECRET")},
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code != 200:
            raise RuntimeError(f"Kick app token: HTTP {r.status_code} {r.text[:200]}")
        d = r.json()
        self._token = {"v": d["access_token"], "exp": time.time() + int(d.get("expires_in", 3600))}
        return self._token["v"]

    def resolve_user_ids(self, slugs: list[str]) -> dict[str, dict]:
        """slug -> {broadcaster_user_id, is_live, viewers, category, title} via GET /channels."""
        out = {}
        for i in range(0, len(slugs), 50):
            r = httpx.get(f"{KICK_API}/channels", params=[("slug", s) for s in slugs[i:i + 50]], timeout=30,
                          headers={"Authorization": f"Bearer {self._app_token()}"})
            if r.status_code != 200:
                raise RuntimeError(f"Kick channels: HTTP {r.status_code} {r.text[:200]}")
            for c in r.json().get("data", []):
                st = c.get("stream") or {}
                out[c["slug"].lower()] = {"broadcaster_user_id": str(c["broadcaster_user_id"]),
                                          "is_live": bool(st.get("is_live")), "viewers": st.get("viewer_count"),
                                          "category": (c.get("category") or {}).get("name"),
                                          "title": c.get("stream_title")}
        return out

    def subscribe(self, broadcaster_user_id: str) -> str | None:
        if broadcaster_user_id in self.subscribed:
            return self.subscribed[broadcaster_user_id]
        r = httpx.post(f"{KICK_API}/events/subscriptions", timeout=30,
                       headers={"Authorization": f"Bearer {self._app_token()}"},
                       json={"broadcaster_user_id": int(broadcaster_user_id), "method": "webhook",
                             "events": [{"name": "chat.message.sent", "version": 1}]})
        if r.status_code not in (200, 201):
            self.last_error = f"subscribe {broadcaster_user_id}: HTTP {r.status_code} {r.text[:200]}"
            raise RuntimeError(self.last_error)
        data = (r.json().get("data") or [{}])[0]
        if data.get("error"):
            self.last_error = f"subscribe {broadcaster_user_id}: {data['error']}"
            raise RuntimeError(self.last_error)
        self.subscribed[broadcaster_user_id] = data.get("subscription_id")
        log.info("subscribed to Kick chat of broadcaster %s", broadcaster_user_id)
        return self.subscribed[broadcaster_user_id]

    def _public_key(self):
        if self._pubkey is None or time.time() - self._pubkey_at > 6 * 3600:
            r = httpx.get(f"{KICK_API}/public-key", timeout=30)
            r.raise_for_status()
            pem = r.json()["data"]["public_key"].encode()
            self._pubkey = serialization.load_pem_public_key(pem)
            self._pubkey_at = time.time()
        return self._pubkey

    def verify(self, headers: dict, body: bytes, public_key=None) -> bool:
        mid = headers.get("kick-event-message-id", "")
        ts = headers.get("kick-event-message-timestamp", "")
        sig = headers.get("kick-event-signature", "")
        if not (mid and ts and sig):
            return False
        try:
            (public_key or self._public_key()).verify(base64.b64decode(sig), f"{mid}.{ts}.".encode() + body,
                                                      padding.PKCS1v15(), hashes.SHA256())
            return True
        except (InvalidSignature, ValueError):
            return False

    def handle_webhook(self, headers: dict, body: bytes, public_key=None) -> tuple[int, str]:
        headers = {k.lower(): v for k, v in headers.items()}
        if not self.verify(headers, body, public_key):
            return 401, "invalid signature"
        mid = headers["kick-event-message-id"]
        if mid in self._seen:
            return 200, "duplicate"
        self._seen[mid] = None
        while len(self._seen) > 20000:
            self._seen.popitem(last=False)
        if headers.get("kick-event-type") != "chat.message.sent":
            return 200, "ignored"
        import json
        d = json.loads(body)
        b = (d.get("broadcaster") or {}).get("user_id")
        sender = (d.get("sender") or {}).get("user_id") or (d.get("sender") or {}).get("username", "")
        self.last_message_at = time.time()
        self.on_message(str(b), _ts(d.get("created_at")), str(sender), d.get("content") or "")
        return 200, "ok"


class YouTubeChat:
    """Polls the live chat of one YouTube live video at a time per streamer."""

    def __init__(self, vault, cfg, db, on_message: Callable[[str, float, str, str], None]):
        self.vault, self.cfg, self.db = vault, cfg, db
        self.on_message = on_message          # (streamer, ts, user, text)
        self.tasks: dict[str, asyncio.Task] = {}
        self.last_error: dict[str, str] = {}

    def configured(self) -> bool:
        return bool(self.vault.get("YT_API_KEY"))

    def _quota_ok(self, units: int) -> bool:
        key = "yt_chat_units:" + time.strftime("%Y-%m-%d", time.gmtime(time.time() - 8 * 3600))  # ~Pacific day
        used = int(self.db.kv_get(key, 0))
        if used + units > int(self.cfg.get("chat.youtube_daily_units") or 8000):
            return False
        self.db.kv_set(key, used + units)
        return True

    def live_chat_id(self, video_id: str) -> tuple[str | None, dict]:
        if not self._quota_ok(1):
            raise RuntimeError("YouTube chat quota budget used up for today")
        r = httpx.get("https://www.googleapis.com/youtube/v3/videos", timeout=30, params={
            "part": "liveStreamingDetails,snippet", "id": video_id, "key": self.vault.get("YT_API_KEY")})
        if r.status_code != 200:
            raise RuntimeError(f"videos.list: HTTP {r.status_code} {r.text[:200]}")
        items = r.json().get("items") or []
        if not items:
            return None, {}
        lsd = items[0].get("liveStreamingDetails") or {}
        return lsd.get("activeLiveChatId"), {"viewers": int(lsd["concurrentViewers"]) if lsd.get("concurrentViewers") else None,
                                             "started_at": _ts(lsd.get("actualStartTime")) if lsd.get("actualStartTime") else None}

    def start(self, streamer: str, video_id: str) -> None:
        if not self.configured() or not video_id or streamer in self.tasks:
            return
        self.tasks[streamer] = asyncio.create_task(self._run(streamer, video_id))

    def stop(self, streamer: str) -> None:
        t = self.tasks.pop(streamer, None)
        if t:
            t.cancel()

    async def _run(self, streamer: str, video_id: str) -> None:
        backoff = 5
        page = None
        chat_id = None
        while True:
            try:
                if not chat_id:
                    chat_id, _ = await asyncio.to_thread(self.live_chat_id, video_id)
                    if not chat_id:
                        self.last_error[streamer] = "live chat disabled or not found"
                        await asyncio.sleep(120)
                        continue
                if not self._quota_ok(5):
                    self.last_error[streamer] = "daily YouTube chat quota budget reached"
                    await asyncio.sleep(600)
                    continue
                params = {"liveChatId": chat_id, "part": "snippet,authorDetails", "maxResults": 2000,
                          "key": self.vault.get("YT_API_KEY")}
                if page:
                    params["pageToken"] = page
                r = await asyncio.to_thread(httpx.get, "https://www.googleapis.com/youtube/v3/liveChat/messages",
                                            params=params, timeout=30)
                if r.status_code != 200:
                    if "liveChatEnded" in r.text or r.status_code == 404:
                        self.last_error[streamer] = "chat ended"
                        return
                    raise RuntimeError(f"liveChatMessages.list HTTP {r.status_code} {r.text[:200]}")
                d = r.json()
                first = page is None
                page = d.get("nextPageToken")
                if not first:  # the first page is backlog from before we joined
                    for it in d.get("items", []):
                        sn = it.get("snippet", {})
                        self.on_message(streamer, _ts(sn.get("publishedAt")),
                                        (it.get("authorDetails") or {}).get("channelId", ""),
                                        sn.get("displayMessage") or "")
                self.last_error.pop(streamer, None)
                # respect YouTube's interval, but never poll faster than the budget allows
                interval = max(d.get("pollingIntervalMillis", 5000) / 1000,
                               float(self.cfg.get("chat.youtube_min_poll_sec") or 8))
                backoff = 5
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error[streamer] = str(e)[:200]
                log.warning("youtube chat %s: %s", streamer, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
