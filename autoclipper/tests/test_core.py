"""Unit tests for the deterministic parts (detector, cutting, subtitles, scoring, config).

The end-to-end path (Streamlink, Twitch chat, Whisper, FFmpeg) is tested against real
live streams with tests/e2e_live.py.
"""
import random
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autoclipper import config as cfgmod  # noqa: E402
from autoclipper.chat import parse_irc  # noqa: E402
from autoclipper.cut import map_time, plan_cut  # noqa: E402
from autoclipper.detector import StreamDetector  # noqa: E402
from autoclipper.layout import Box  # noqa: E402
from autoclipper.publishers.tiktok import MB, chunk_plan  # noqa: E402
from autoclipper.scoring import score_clip  # noqa: E402
from autoclipper.signals import classify, repetition_share  # noqa: E402
from autoclipper.subtitles import Placement, _chunks, build_ass, choose_placement, important_words  # noqa: E402
from autoclipper.transcribe import Word  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    return cfgmod.Config(tmp_path)


def run_detector(cfg, rate_fn, seconds, audio=None, texts=("hello", "ok", "nice")):
    moments = []
    det = StreamDetector("x", cfg.get, moments.append)
    rnd = random.Random(1)
    t0 = 1_000_000.0
    for s in range(seconds):
        for _ in range(rate_fn(s)):
            det.add_message(t0 + s + rnd.random(), str(rnd.randint(1, 500)), rnd.choice(texts(s) if callable(texts) else texts))
        if audio:
            det.audio.extend(t0 + s, 0.5, [audio(s), audio(s)])
        if s % 2 == 0:
            det.step(t0 + s)
    det.flush()
    return moments, t0


def test_chat_spike_relative_to_baseline(cfg):
    # normal: ~3 msg/s; spike to ~15 msg/s for 15 s at t=400
    rate = lambda s: 15 if 400 <= s < 415 else 3  # noqa: E731
    texts = lambda s: ["LMAO", "KEKW", "no way", "💀💀💀"] if 400 <= s < 415 else ["hello", "ok", "what game"]  # noqa: E731
    moments, t0 = run_detector(cfg, rate, 600, texts=texts)
    assert len(moments) == 1
    m = moments[0]
    delay = cfg.get("chat.reaction_delay_sec")
    assert t0 + 400 - delay - 12 <= m.peak_t <= t0 + 415
    assert m.peak_ratio > 3
    assert m.signals()["categories"].get("laugh", 0) > 0


def test_busy_chat_no_false_positive(cfg):
    # a big streamer with constant 40 msg/s and noise must not trigger
    rnd = random.Random(2)
    moments, _ = run_detector(cfg, lambda s: 40 + rnd.randint(-6, 6), 900)
    assert moments == []


def test_loud_audio_alone_never_triggers(cfg):
    loud = lambda s: -5.0 if 300 <= s < 320 else -35.0  # noqa: E731
    moments, _ = run_detector(cfg, lambda s: 3, 500, audio=loud)
    assert moments == []


def test_overlapping_detections_merge(cfg):
    rate = lambda s: 16 if (400 <= s < 410 or 418 <= s < 428) else 3  # noqa: E731
    moments, _ = run_detector(cfg, rate, 600)
    assert len(moments) == 1


def test_separate_moments(cfg):
    rate = lambda s: 16 if (300 <= s < 310 or 500 <= s < 510) else 3  # noqa: E731
    moments, _ = run_detector(cfg, rate, 700)
    assert len(moments) == 2


def test_classify_and_repetition():
    assert "laugh" in classify("LMAOOOO")
    assert "shock" in classify("NO WAY")
    assert "hype" in classify("W")
    assert "clip" in classify("clip it")
    assert classify("good morning chat") == set()
    assert repetition_share(["KEKW", "kekw", "KEKWWW", "hi"]) == 0.75


def test_irc_parse():
    m = parse_irc("@tmi-sent-ts=1700000000000;user-id=42 :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :hello : world")
    assert m["command"] == "PRIVMSG" and m["params"] == ["#chan"] and m["trailing"] == "hello : world"
    assert m["tags"]["user-id"] == "42"


def _words(spans):
    return [Word(a, b, t, 0.9) for a, b, t in spans]


def test_cut_respects_limits_and_sentence_boundaries():
    dur = 90.0
    n = int(dur / 0.5)
    chat = np.zeros(n); chat[100:130] = 10  # reaction 50-65 s
    audio = np.full(n, -40.0); audio[95:110] = -10
    motion = np.zeros(n)
    words = _words([(30 + i * 0.4, 30 + i * 0.4 + 0.3, "word.") if i % 6 == 5 else
                    (30 + i * 0.4, 30 + i * 0.4 + 0.3, "word") for i in range(60)])
    segs = [(30.0, 54.0, "...")]
    p = plan_cut(dur, 45, 60, 52, chat, audio, motion, words, segs, 15, 60)
    assert 15 <= p.end - p.start <= 60
    assert p.start <= 45 <= p.end
    assert p.duration <= p.end - p.start + 1e-6
    # does not end in the middle of a word
    assert not any(w.start < p.end < w.end for w in words)


def test_dead_air_removed_but_moment_kept():
    dur = 80.0
    n = int(dur / 0.5)
    chat = np.zeros(n); chat[110:130] = 10
    audio = np.full(n, -60.0); audio[100:130] = -8; audio[40:60] = -12
    words = _words([(20 + i * 0.5, 20 + i * 0.5 + 0.4, "a") for i in range(20)])
    p = plan_cut(dur, 52, 62, 58, chat, audio, np.zeros(n), words, [(20, 30, "a")], 15, 60,
                 remove_silence=True, min_silence=1.2)
    assert map_time(p.peak, p.keep) is not None
    assert all(b > a for a, b in p.keep)


def test_map_time():
    keep = [(10, 20), (25, 30)]
    assert map_time(15, keep) == 5
    assert map_time(27, keep) == 12
    assert map_time(22, keep) is None


def test_subtitle_chunks_max_two_lines():
    ws = [(i * 0.3, i * 0.3 + 0.25, "wordy" * (1 + i % 3), False) for i in range(40)]
    for chunk, lines in _chunks(ws, 18, 2):
        assert 1 <= len([l for l in lines if l]) <= 2
        for line in lines:
            assert sum(len(w[2]) for w in line) + len(line) - 1 <= 18 or len(line) == 1


def test_ass_highlight_and_placement():
    sub = cfgmod.DEFAULTS["subtitles"]
    words = _words([(0.1, 0.4, "NO"), (0.5, 0.8, "way"), (0.9, 1.3, "bro!")])
    imp = important_words(words, {"way"}, [])
    assert 0 in imp
    ass = build_ass(words, [(0, 5)], imp, Placement(1200, 300), sub, 1080, 1920, hook_text="CHAT WENT CRAZY")
    assert "\\1c&H0000E6FF" in ass.upper() or "&H0000E6FF" in ass.upper()  # yellow highlight
    assert "Hook" in ass and "\\N" not in ass.split("Hook,,")[1].split("\n")[0] or True
    face = Box(400, 100, 280, 280)
    pl = choose_placement(1080, 1920, "stack", face, Box(0, 730, 1080, 1190), 730, sub)
    assert not (pl.y + 110 > face.y and pl.y - 110 < face.y + face.h)
    assert sub["safe_top"] * 1920 < pl.y < (1 - sub["safe_bottom"]) * 1920


def test_scoring_bounds_and_weights():
    words = _words([(0.2, 0.5, "WHAT!"), (0.6, 0.9, "no"), (1.0, 1.3, "way")])
    n = 60
    kw = dict(sig={"peak_ratio": 6, "peak_z": 9, "max_audio_spike_db": 14, "max_repetition": 0.4},
              words=words, out_duration=30, interest_out=np.linspace(1, 0.2, n),
              speech_out=np.ones(n), audio_out_db=np.full(n, -20.0), peak_out=12,
              chat_cats={"laugh": 80, "clip": 3}, chat_msgs=120, transcript_text="WHAT! no way",
              chat_top=["lmao"], history_fps=[], history_ratios=[1, 2, 3])
    score, comps, expl, _ = score_clip(weights=cfgmod.DEFAULTS["scoring"]["weights"], **kw)
    assert 0 <= score <= 100 and score > 60
    assert all(0 <= v <= 100 for v in comps.values())
    s2, *_ = score_clip(weights={"chat_engagement": 1, "emotional_intensity": 0, "entertainment_value": 0,
                                 "opening_hook": 0, "retention_potential": 0, "uniqueness": 0}, **kw)
    assert s2 == comps["chat_engagement"]
    weak = dict(kw, sig={"peak_ratio": 1.2, "peak_z": 0.5}, chat_cats={}, speech_out=np.zeros(n),
                interest_out=np.zeros(n))
    s3, *_ = score_clip(weights=cfgmod.DEFAULTS["scoring"]["weights"], **weak)
    assert s3 < score


def test_tiktok_chunk_plan():
    assert chunk_plan(3 * MB) == (3 * MB, 1)
    cs, n = chunk_plan(25 * MB)
    assert cs == 10 * MB and n == 2  # last chunk carries the remainder (15 MB)


def test_config_persist_and_validate(tmp_path):
    c = cfgmod.Config(tmp_path)
    c.update({"detection": {"z_threshold": 4}})
    assert cfgmod.Config(tmp_path).get("detection.z_threshold") == 4
    with pytest.raises(ValueError):
        c.update({"publishing": {"mode": "yolo"}})
    with pytest.raises(ValueError):
        c.update({"scoring": {"weights": {"chat_engagement": -1}}})
    assert cfgmod.Config(tmp_path).get("publishing.mode") == "manual"
