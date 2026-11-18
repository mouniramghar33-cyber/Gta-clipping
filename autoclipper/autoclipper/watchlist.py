"""Exclusive streamer watchlist: seeds the 11 verified streamers and their channels."""
from __future__ import annotations

import time
from pathlib import Path

import yaml

WATCHLIST_FILE = Path(__file__).with_name("watchlist.yaml")


def load(path: Path = WATCHLIST_FILE) -> list[dict]:
    return yaml.safe_load(path.read_text())["streamers"]


def sync(db, path: Path = WATCHLIST_FILE) -> int:
    """Insert missing watchlist streamers/channels. Never touches user settings (enabled,
    rights) of existing rows and never adds anything that isn't in the file."""
    n = 0
    for s in load(path):
        if not db.one("SELECT login FROM streamers WHERE login=?", (s["id"],)):
            db.execute("INSERT INTO streamers (login, platform, display_name, name, aka, source, enabled, "
                       "rights_granted, added_at) VALUES (?,?,?,?,?,?,1,0,?)",
                       (s["id"], s["channels"][0]["platform"], s["name"], s["name"],
                        __import__("json").dumps(s.get("aka", [])), "watchlist", time.time()))
            n += 1
        else:
            db.execute("UPDATE streamers SET name=?, aka=?, source='watchlist' WHERE login=?",
                       (s["name"], __import__("json").dumps(s.get("aka", [])), s["id"]))
        for prio, ch in enumerate(s["channels"]):
            db.execute("INSERT OR IGNORE INTO streamer_channels (streamer, platform, handle, priority, note) "
                       "VALUES (?,?,?,?,?)", (s["id"], ch["platform"], ch["handle"], prio, ch.get("note")))
    return n


def ensure_channel(db, login: str, platform: str) -> None:
    """Streamers added by hand in the dashboard get a single channel on their platform."""
    if not db.channels(login):
        db.execute("INSERT OR IGNORE INTO streamer_channels (streamer, platform, handle, priority) VALUES (?,?,?,0)",
                   (login, platform, login))


def name_index(db) -> dict[str, str]:
    """lowercase name/alias/handle -> streamer id (used for collaboration detection)."""
    import json
    idx = {}
    for r in db.query("SELECT login, name, aka FROM streamers"):
        aka = r["aka"] if isinstance(r["aka"], list) else json.loads(r["aka"] or "[]")
        for a in [r["login"], r["name"] or "", *aka]:
            a = a.lower().strip()
            if len(a) >= 4:  # avoid matching tiny words like "kai" inside other words
                idx[a] = r["login"]
        for c in db.channels(r["login"]):
            if c["platform"] != "youtube":
                idx[c["handle"].lower()] = r["login"]
    return idx
