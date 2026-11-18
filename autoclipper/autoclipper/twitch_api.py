"""Optional Twitch Helix integration (free app credentials).

With TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET set, one Helix call checks up to 100
streamers at once and returns the real stream start time (used for VOD timestamps).
Without them, live checks fall back to Streamlink.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import httpx

_token: dict = {"value": None, "exp": 0.0}
_get = os.environ.get


def configure(getter) -> None:
    """Read credentials through the vault (env first, then values saved in the setup wizard)."""
    global _get
    _get = getter


def enabled() -> bool:
    return bool(_get("TWITCH_CLIENT_ID") and _get("TWITCH_CLIENT_SECRET"))


def _app_token() -> str:
    if _token["value"] and time.time() < _token["exp"] - 300:
        return _token["value"]
    r = httpx.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": _get("TWITCH_CLIENT_ID"), "client_secret": _get("TWITCH_CLIENT_SECRET"),
        "grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    d = r.json()
    _token.update(value=d["access_token"], exp=time.time() + d.get("expires_in", 3600))
    return d["access_token"]


def live_streams(logins: list[str]) -> dict[str, dict]:
    """login -> {title, category, started_at, viewers} for those that are live."""
    out: dict[str, dict] = {}
    for i in range(0, len(logins), 100):
        batch = logins[i:i + 100]
        r = httpx.get("https://api.twitch.tv/helix/streams",
                      params=[("user_login", l) for l in batch] + [("first", "100")],
                      headers={"Client-Id": _get("TWITCH_CLIENT_ID"),
                               "Authorization": f"Bearer {_app_token()}"}, timeout=30)
        r.raise_for_status()
        for s in r.json().get("data", []):
            if s.get("type") != "live":
                continue
            out[s["user_login"].lower()] = {
                "title": s.get("title"), "category": s.get("game_name"), "author": s.get("user_name"),
                "viewers": s.get("viewer_count"),
                "started_at": datetime.fromisoformat(s["started_at"].replace("Z", "+00:00")).timestamp(),
            }
    return out
