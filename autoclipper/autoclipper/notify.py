"""Optional Telegram notifications (Bot API sendMessage).

Needs TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID (your chat with the bot).
Each event type can be switched on/off under Settings -> notifications.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg, db, vault):
        self.cfg, self.db, self.vault = cfg, db, vault
        self._recent: dict[str, float] = {}

    def configured(self) -> bool:
        return bool(self.vault.get("TELEGRAM_BOT_TOKEN") and self.vault.get("TELEGRAM_CHAT_ID"))

    def send(self, event: str, text: str, force: bool = False) -> bool:
        n = self.cfg["notifications"]
        if not force and (not n.get("telegram") or not n.get("events", {}).get(event, False)):
            return False
        if not self.configured():
            return False
        key = f"{event}:{text[:80]}"
        if not force and time.time() - self._recent.get(key, 0) < 600:
            return False  # same message within 10 min: don't spam
        self._recent[key] = time.time()
        try:
            r = httpx.post(f"https://api.telegram.org/bot{self.vault.get('TELEGRAM_BOT_TOKEN')}/sendMessage",
                           json={"chat_id": self.vault.get("TELEGRAM_CHAT_ID"), "text": text[:4000],
                                 "disable_web_page_preview": True}, timeout=20)
            if r.status_code != 200:
                log.warning("telegram returned %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except httpx.HTTPError as e:
            log.warning("telegram failed: %s", e)
            return False

    def test(self) -> tuple[bool, str]:
        if not self.configured():
            return False, "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required"
        try:
            r = httpx.post(f"https://api.telegram.org/bot{self.vault.get('TELEGRAM_BOT_TOKEN')}/sendMessage",
                           json={"chat_id": self.vault.get("TELEGRAM_CHAT_ID"),
                                 "text": "✅ AutoClipper notifications are working."}, timeout=20)
            return r.status_code == 200, r.json().get("description", "ok") if r.status_code != 200 else "sent"
        except httpx.HTTPError as e:
            return False, str(e)

    def daily_summary_due(self) -> bool:
        tz = ZoneInfo(self.cfg.get("publishing.timezone"))
        now = datetime.now(tz)
        today = now.strftime("%Y-%m-%d")
        if now.hour < int(self.cfg.get("notifications.daily_summary_hour")):
            return False
        return self.db.kv_get("daily_summary_sent") != today

    def daily_summary(self) -> str:
        tz = ZoneInfo(self.cfg.get("publishing.timezone"))
        end = datetime.combine(datetime.now(tz).date(), datetime.min.time(), tz)
        a, b = (end - timedelta(days=1)).timestamp(), end.timestamp()
        q = lambda sql, *p: self.db.one(sql, p)["n"]  # noqa: E731
        clips = q("SELECT COUNT(*) n FROM clips WHERE created_at>=? AND created_at<?", a, b)
        prio = q("SELECT COUNT(*) n FROM clips WHERE created_at>=? AND created_at<? AND priority=1", a, b)
        pub = q("SELECT COUNT(*) n FROM publish_jobs WHERE status='published' AND published_at>=? AND published_at<?", a, b)
        failed = q("SELECT COUNT(*) n FROM publish_jobs WHERE status='failed' AND updated_at>=? AND updated_at<?", a, b)
        waiting = q("SELECT COUNT(*) n FROM publish_jobs WHERE status='awaiting_approval'")
        top = self.db.one("""SELECT j.title, j.url, m.views FROM metrics m JOIN publish_jobs j ON j.id=m.job_id
                             WHERE m.views IS NOT NULL ORDER BY m.fetched_at DESC, m.views DESC LIMIT 1""")
        lines = [f"📊 AutoClipper - {(end - timedelta(days=1)).strftime('%a %d %b')}",
                 f"Clips made: {clips} ({prio} priority)", f"Published: {pub}   Failed: {failed}",
                 f"Waiting for your approval: {waiting}"]
        if top:
            lines.append(f"Top recent post: {top['views']:,} views - {top['title']}\n{top['url'] or ''}")
        return "\n".join(lines)
