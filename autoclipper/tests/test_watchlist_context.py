"""Tests for the watchlist, context understanding, duration rules, tiers, Kick webhook
verification and the no-chat / GTA VI detector modes."""
import base64
import json
import random
import sys
from pathlib import Path

import numpy as np
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autoclipper import watchlist  # noqa: E402
from autoclipper.chat_sources import KickChat  # noqa: E402
from autoclipper.config import Config  # noqa: E402
from autoclipper.context import classify, is_gta  # noqa: E402
from autoclipper.cut import plan_cut  # noqa: E402
from autoclipper.db import DB  # noqa: E402
from autoclipper.detector import StreamDetector  # noqa: E402
from autoclipper.pipeline import tier_for  # noqa: E402
from autoclipper.transcribe import Word  # noqa: E402


def test_watchlist_is_exactly_the_11_verified_streamers(tmp_path):
    db = DB(tmp_path / "w.db")
    assert watchlist.sync(db) == 11
    assert watchlist.sync(db) == 0                      # idempotent, never adds anything else
    ids = {r["login"] for r in db.streamers()}
    assert ids == {"marlon", "lacy", "stableronaldo", "ishowspeed", "kaicenat", "jynxzi", "jasontheween",
                   "adinross", "rakai", "tota", "typicalgamer"}
    assert db.channels("rakai")[0]["handle"] == "2xrakai"          # not twitch.tv/rakai (different person)
    assert db.channels("tota")[0]["handle"] == "totaamc"
    assert db.channels("ishowspeed")[0] == {**db.channels("ishowspeed")[0], "platform": "youtube",
                                             "handle": "UCWsDFcIhY2DBi3GB5uykGXA"}
    assert [c["platform"] for c in db.channels("adinross")] == ["kick"]
    # unverified Kick name-squatters are not monitored
    assert not any(c["platform"] == "kick" for c in db.channels("kaicenat"))
    # user settings survive a re-sync
    db.execute("UPDATE streamers SET rights_granted=1, enabled=0 WHERE login='lacy'")
    watchlist.sync(db)
    r = db.one("SELECT * FROM streamers WHERE login='lacy'")
    assert r["rights_granted"] == 1 and r["enabled"] == 0


def test_collab_detection_and_name_index(tmp_path):
    db = DB(tmp_path / "w.db")
    watchlist.sync(db)
    names = watchlist.name_index(db)
    assert names["2xrakai"] == "rakai" and names["kai cenat"] == "kaicenat"
    assert "speed" not in names and "kai" not in names           # no ambiguous short words
    r = classify(streamer="kaicenat", transcript="yo speed come here bro", chat_texts=["IShowSpeed is here"] * 5,
                 chat_cats={}, chat_msgs=5, sig={}, emotions={}, category="Just Chatting",
                 title="w/ @ishowspeed", names=names, peak_ratio=2.0)
    assert r.collab == ["ishowspeed"] and "collab" in r.labels


def test_context_labels_and_ordinary_conversation(tmp_path):
    db = DB(tmp_path / "w.db")
    watchlist.sync(db)
    names = watchlist.name_index(db)
    funny = classify(streamer="kaicenat", transcript="NO WAY haha haha I'm crying bro what",
                     chat_texts=["LMAO", "KEKW", "💀"] * 10, chat_cats={"laugh": 25, "shock": 5}, chat_msgs=30,
                     sig={"max_repetition": 0.3, "max_audio_spike_db": 13}, emotions={"happy": 0.7},
                     category="Just Chatting", title="", names=names, peak_ratio=3.5)
    assert funny.entertaining and funny.primary in ("funny", "reaction")
    assert any("laughing" in e for e in funny.evidence["funny"])
    boring = classify(streamer="kaicenat", transcript="so yeah I was thinking about getting food later",
                      chat_texts=["hi", "hello"], chat_cats={}, chat_msgs=2, sig={}, emotions={},
                      category="Just Chatting", title="", names=names, peak_ratio=1.6)
    assert boring.entertaining is False


def test_gta_detection_from_category():
    assert is_gta("Grand Theft Auto VI") and is_gta("Grand Theft Auto VI (GTA)")
    assert not is_gta("Grand Theft Auto V") and not is_gta("Just Chatting")
    r = classify(streamer="lacy", transcript="bro the cops are chasing us, why is this NPC so real",
                 chat_texts=["GTA 6 IS REAL"], chat_cats={"shock": 3}, chat_msgs=10, sig={}, emotions={},
                 category="Grand Theft Auto VI", title="GTA 6", names={}, peak_ratio=3)
    assert r.gta and "police_chase" in r.gta_tags and "npc" in r.gta_tags


def _words_every(start, end, gap=0.45):
    t, out = start, []
    while t < end:
        out.append(Word(t, t + 0.35, "word.", 0.9))
        t += gap
    return out


@pytest.mark.parametrize("reaction_len", [5, 25, 70])
def test_duration_rules(reaction_len):
    """Ideal 20-40 s, trimmed toward 45 s, never above 60 s, short moments stay short."""
    dur = 150.0
    n = int(dur / 0.5)
    ev0, ev1 = 40.0, 40.0 + reaction_len
    chat = np.zeros(n); chat[int(ev0 / 0.5):int(ev1 / 0.5)] = 10
    audio = np.full(n, -30.0); audio[int(ev0 / 0.5):int(ev1 / 0.5)] = -8
    words = _words_every(5, dur - 5)
    p = plan_cut(dur, ev0, ev1, ev0 + 2, chat, audio, np.zeros(n), words, [(5, dur - 5, "x")], 8, 60,
                 ideal=(20, 40), preferred_max=45, hard_max=60)
    length = p.end - p.start
    assert length <= 60 + 1e-6 and p.duration <= 60 + 1e-6
    if reaction_len == 70:
        assert length <= 45.5            # long reactions are trimmed toward the preferred max
    if reaction_len == 5:
        assert length <= 40              # never padded far beyond the moment


def test_hard_cap_even_with_bad_config():
    dur = 200.0
    n = int(dur / 0.5)
    chat = np.full(n, 10.0)
    p = plan_cut(dur, 10, 190, 100, chat, np.full(n, -10.0), np.zeros(n), _words_every(0, dur), [(0, dur, "x")],
                 8, 180, ideal=(20, 40), preferred_max=45, hard_max=60)
    assert p.end - p.start <= 60 + 1e-6


def test_tiers():
    tiers = {"immediate": 95, "very_high": 90, "high": 85, "normal": 80, "review": 70}
    assert [tier_for(s, tiers) for s in (97, 92, 86, 81, 75, 50)] == \
        ["immediate", "very_high", "high", "normal", "review", "reject"]


def test_kick_webhook_signature_and_dedupe():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    got = []
    k = KickChat(vault=None, on_message=lambda *a: got.append(a))
    body = json.dumps({"broadcaster": {"user_id": 875396}, "sender": {"user_id": 7, "username": "x"},
                       "content": "LMAO", "created_at": "2026-11-19T10:00:00Z"}).encode()
    h = {"Kick-Event-Message-Id": "01ABC", "Kick-Event-Message-Timestamp": "2026-11-19T10:00:00Z",
         "Kick-Event-Type": "chat.message.sent"}
    sig = key.sign(f"01ABC.2026-11-19T10:00:00Z.".encode() + body, padding.PKCS1v15(), hashes.SHA256())
    h["Kick-Event-Signature"] = base64.b64encode(sig).decode()
    assert k.handle_webhook(h, body, key.public_key()) == (200, "ok")
    assert got[0][0] == "875396" and got[0][3] == "LMAO"
    assert k.handle_webhook(h, body, key.public_key()) == (200, "duplicate")
    assert k.handle_webhook(h, body + b" ", key.public_key())[0] == 401       # tampered body
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert k.handle_webhook({**h, "Kick-Event-Message-Id": "01ABD"}, body, other.public_key())[0] == 401


def _run(cfg, has_chat, audio, expr, body, seconds=400, sensitivity=1.0, rate=None):
    moments = []
    det = StreamDetector("x", cfg.get, moments.append)
    det.has_chat = has_chat
    det.sensitivity = sensitivity
    rnd = random.Random(3)
    t0 = 1_000_000.0
    for s in range(seconds):
        if rate:
            for _ in range(rate(s)):
                det.add_message(t0 + s + rnd.random(), str(rnd.randint(1, 400)), "lol")
        det.audio.extend(t0 + s, 0.5, [audio(s)] * 2)
        if s % 2 == 0:
            det.expr.extend(t0 + s, 1, [expr(s)])
            det.body.extend(t0 + s, 1, [body(s)])
            det.step(t0 + s)
    det.flush()
    return moments


def test_nochat_mode_needs_a_visible_reaction(tmp_path):
    cfg = Config(tmp_path)
    loud = lambda s: -5.0 if 200 <= s < 210 else -35.0  # noqa: E731
    calm_face = lambda s: 0.1  # noqa: E731
    still = lambda s: 5.0  # noqa: E731
    assert _run(cfg, False, loud, calm_face, still) == []                     # loud alone: nothing
    react = lambda s: 0.8 if 200 <= s < 210 else 0.1  # noqa: E731
    move = lambda s: 20.0 if 200 <= s < 210 else 5.0  # noqa: E731
    assert len(_run(cfg, False, loud, react, move)) == 1                     # face + body + audio


def test_gta_sensitivity_lowers_the_bar(tmp_path):
    cfg = Config(tmp_path)
    rate = lambda s: 6 if 300 <= s < 310 else 3  # noqa: E731   # a modest 2x spike
    flat = lambda s: -30.0  # noqa: E731
    zero = lambda s: 0.1  # noqa: E731
    normal = _run(cfg, True, flat, zero, lambda s: 5.0, 500, 1.0, rate)
    gta = _run(cfg, True, flat, zero, lambda s: 5.0, 500, 0.75, rate)
    assert len(gta) >= len(normal) and len(gta) >= 1
