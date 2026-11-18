"""Real-time Twitch chat via Twitch's official IRC-over-WebSocket endpoint.

Read-only anonymous login (justinfan) needs no credentials. One connection serves
all channels. It reconnects automatically with exponential backoff.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Callable

import websockets

log = logging.getLogger(__name__)

WS_URL = "wss://irc-ws.chat.twitch.tv:443"


def parse_irc(line: str) -> dict | None:
    tags: dict[str, str] = {}
    if line.startswith("@"):
        raw, _, line = line.partition(" ")
        for kv in raw[1:].split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing = None
    if " :" in line:
        line, _, trailing = line.partition(" :")
    parts = line.split()
    if not parts:
        return None
    return {"tags": tags, "prefix": prefix, "command": parts[0], "params": parts[1:], "trailing": trailing}


class TwitchChat:
    def __init__(self, on_message: Callable[[str, float, str, str], None]):
        self.on_message = on_message
        self.channels: set[str] = set()
        self._ws = None
        self.connected = False
        self.last_message_at = 0.0
        self.reconnects = 0

    async def join(self, channel: str) -> None:
        channel = channel.lower()
        self.channels.add(channel)
        if self._ws is not None and self.connected:
            await self._send(f"JOIN #{channel}")

    async def part(self, channel: str) -> None:
        channel = channel.lower()
        self.channels.discard(channel)
        if self._ws is not None and self.connected:
            await self._send(f"PART #{channel}")

    async def _send(self, msg: str) -> None:
        try:
            await self._ws.send(msg)
        except Exception as e:  # noqa: BLE001
            log.warning("chat send failed: %s", e)

    async def run_forever(self) -> None:
        backoff = 1
        while True:
            started = time.time()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("chat connection lost: %s", e)
            self.connected = False
            self.reconnects += 1
            backoff = 1 if time.time() - started > 60 else min(backoff * 2, 60)
            await asyncio.sleep(backoff + random.random())

    async def _session(self) -> None:
        async with websockets.connect(WS_URL, ping_interval=60, ping_timeout=30, max_size=2**22) as ws:
            self._ws = ws
            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await ws.send("PASS SCHMOOPIIE")
            await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
            joined: set[str] = set()
            # Twitch limits joins; send them in small batches.
            async def join_all():
                for ch in sorted(self.channels):
                    await ws.send(f"JOIN #{ch}")
                    joined.add(ch)
                    await asyncio.sleep(0.6)
            self.connected = True
            log.info("chat connected, joining %d channels", len(self.channels))
            asyncio.create_task(join_all())
            async for frame in ws:
                for line in str(frame).split("\r\n"):
                    if not line:
                        continue
                    m = parse_irc(line)
                    if not m:
                        continue
                    cmd = m["command"]
                    if cmd == "PING":
                        await ws.send(f"PONG :{m['trailing'] or 'tmi.twitch.tv'}")
                    elif cmd == "PRIVMSG" and m["params"]:
                        ch = m["params"][0].lstrip("#")
                        ts = time.time()
                        sent = m["tags"].get("tmi-sent-ts")
                        if sent and sent.isdigit():
                            ts = int(sent) / 1000.0
                        user = m["tags"].get("user-id") or m["prefix"].split("!")[0]
                        self.last_message_at = time.time()
                        self.on_message(ch, ts, user, m["trailing"] or "")
                    elif cmd == "RECONNECT":
                        log.info("chat server requested reconnect")
                        return
