"""Official-API publishers. Credentials come from environment variables only."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


class TokenStore:
    """Persists rotating OAuth tokens (e.g. TikTok refresh tokens) in a 0600 JSON file."""

    _lock = threading.Lock()

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)

    def get(self, key: str) -> str | None:
        with self._lock:
            if self.path.exists():
                return json.loads(self.path.read_text()).get(key)
        return None

    def set(self, key: str, value: str) -> None:
        with self._lock:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
            data[key] = value
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            tmp.replace(self.path)


def caption_for(clip: dict, credit: str, max_len: int = 2200) -> str:
    tags = " ".join(f"#{t}" for t in (clip.get("hashtags") or []))
    text = f"{clip['title']}\n\n{credit}\n{tags}"
    return text[:max_len]


class Publisher:
    name = "base"

    def configured(self) -> bool:
        raise NotImplementedError

    def publish(self, clip: dict, video: Path, public_url: str | None, cfg) -> tuple[str, str | None]:
        """Upload; returns (remote_id, url)."""
        raise NotImplementedError


def all_publishers(token_store: TokenStore) -> dict[str, Publisher]:
    from .instagram import InstagramPublisher
    from .tiktok import TikTokPublisher
    from .youtube import YouTubePublisher
    return {p.name: p for p in (YouTubePublisher(), TikTokPublisher(token_store), InstagramPublisher())}


def wait(seconds: float) -> None:
    time.sleep(seconds)
