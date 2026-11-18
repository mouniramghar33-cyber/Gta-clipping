"""Encrypted secret storage.

- OAuth tokens and credentials entered in the setup wizard are encrypted with Fernet
  (AES-128-CBC + HMAC-SHA256) before they touch the database.
- The key comes from AUTOCLIPPER_SECRET_KEY, or is generated once into
  <data>/secrets/master.key (mode 0600). The key and the database live in separate files,
  so a leaked database alone reveals nothing.
- Environment variables always take precedence over values stored via the wizard.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# Credentials that can be entered in the setup wizard (non-listed names are rejected).
WIZARD_SECRETS = {
    "YT_CLIENT_ID", "YT_CLIENT_SECRET",
    "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET",
    "BUFFER_API_KEY", "UPLOADPOST_API_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET",
    "KICK_CLIENT_ID", "KICK_CLIENT_SECRET", "YT_API_KEY",
}


class Vault:
    _lock = threading.Lock()

    def __init__(self, data_dir: Path, db):
        self.db = db
        key = os.environ.get("AUTOCLIPPER_SECRET_KEY", "").strip()
        if not key:
            kdir = Path(data_dir) / "secrets"
            kdir.mkdir(parents=True, exist_ok=True)
            os.chmod(kdir, 0o700)
            kfile = kdir / "master.key"
            with self._lock:
                if not kfile.exists():
                    fd = os.open(kfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(Fernet.generate_key())
            key = kfile.read_bytes().decode().strip()
        self.f = Fernet(key.encode() if isinstance(key, str) else key)

    # ---- generic encryption ----------------------------------------------
    def encrypt(self, data: dict) -> str:
        return self.f.encrypt(json.dumps(data).encode()).decode()

    def decrypt(self, token: str | None) -> dict:
        if not token:
            return {}
        try:
            return json.loads(self.f.decrypt(token.encode()))
        except InvalidToken as e:
            raise RuntimeError("stored secret cannot be decrypted (wrong AUTOCLIPPER_SECRET_KEY?)") from e

    # ---- named credentials (env first, then encrypted DB) ---------------------
    def get(self, name: str) -> str | None:
        v = os.environ.get(name)
        if v:
            return v
        row = self.db.one("SELECT value_enc FROM secrets WHERE name=?", (name,))
        return self.decrypt(row["value_enc"]).get("v") if row else None

    def set(self, name: str, value: str) -> None:
        if name not in WIZARD_SECRETS:
            raise ValueError(f"{name} cannot be set from the dashboard")
        if value == "":
            self.db.execute("DELETE FROM secrets WHERE name=?", (name,))
            return
        self.db.execute("INSERT OR REPLACE INTO secrets (name, value_enc, updated_at) VALUES (?,?,?)",
                        (name, self.encrypt({"v": value}), time.time()))

    def status(self) -> dict:
        """Which credentials are present and where they come from (never the values)."""
        stored = {r["name"] for r in self.db.query("SELECT name FROM secrets")}
        return {n: ("env" if os.environ.get(n) else "stored" if n in stored else None) for n in sorted(WIZARD_SECRETS)}
