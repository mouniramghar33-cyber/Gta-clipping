"""Virality score (0-100) from measured signals, with configurable weights.

Every component comes from something measured (chat, audio, transcript, cut). Nothing is
guessed. The explanation lists the evidence behind the score.
"""
from __future__ import annotations

import math
import re

import numpy as np

STOP = set("""a an the and or but if of to in on at for with is are was were be been it its this that
these those i you he she we they me him her us them my your his our their so just like yeah oh um uh
do did does dont don't im i'm it's that's what's gonna got get go going not no yes ok okay""".split())


def fingerprint(transcript: str, chat_tokens: list[str]) -> list[str]:
    words = {w for w in re.findall(r"[a-z0-9']+", transcript.lower()) if w not in STOP and len(w) > 2}
    return sorted(words | {t for t in chat_tokens if len(t) > 1})


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_clip(*, sig: dict, words, out_duration: float, interest_out: np.ndarray,
               speech_out: np.ndarray, audio_out_db: np.ndarray, peak_out: float,
               chat_cats: dict, chat_msgs: int, transcript_text: str, chat_top: list[str],
               history_fps: list[list[str]], history_ratios: list[float], weights: dict,
               step: float = 0.5) -> tuple[float, dict, list[str], list[str]]:
    expl: list[str] = []

    # 1. Chat engagement: how far above the streamer's normal chat this went
    ratio, z = sig.get("peak_ratio", 1.0), sig.get("peak_z", 0.0)
    base = 1 - math.exp(-max(ratio - 1, 0) / 2.2)
    chat = 100 * (0.65 * base + 0.35 * clamp01(z / 8))
    expl.append(f"chat peaked at {ratio:.1f}x this streamer's normal activity (z={z:.1f}, "
                f"{sig.get('max_messages', 0)} msgs / 10 s from {sig.get('max_users', 0)} people)")

    # 2. Emotional intensity: audio spike + emphatic speech + emotional chat
    spike = sig.get("max_audio_spike_db", 0.0)
    n_words = max(len(words), 1)
    emph = sum(1 for w in words if w.text.endswith("!") or (w.text.isupper() and len(w.text) > 1))
    shock_laugh = (chat_cats.get("shock", 0) + chat_cats.get("laugh", 0)) / max(chat_msgs, 1)
    emo = 100 * (0.45 * clamp01(spike / 15) + 0.25 * clamp01(emph / (n_words * 0.12 + 1))
                 + 0.30 * clamp01(shock_laugh * 1.6))
    if spike >= 6:
        expl.append(f"audio jumped {spike:.0f} dB above the stream's normal level")

    # 3. Entertainment: what kind of reaction chat had
    shares = {k: chat_cats.get(k, 0) / max(chat_msgs, 1) for k in
              ("laugh", "shock", "hype", "fail", "tension")}
    top_cat = max(shares, key=shares.get) if shares else None
    rep = sig.get("max_repetition", 0.0)
    clip_req = chat_cats.get("clip", 0)
    laugh_speech = bool(re.search(r"\b(ha(ha)+|lmao|lol)\b", transcript_text.lower()))
    ent = 100 * clamp01(1.1 * max(shares.values() or [0]) + 0.5 * rep + 0.15 * min(clip_req, 3)
                        + (0.1 if laugh_speech else 0))
    if top_cat and shares[top_cat] > 0.08:
        expl.append(f"{shares[top_cat]:.0%} of chat messages were {top_cat} reactions")
    if rep >= 0.2:
        expl.append(f"chat spammed the same message ({rep:.0%} identical)")
    if clip_req:
        expl.append(f"viewers asked to clip it {clip_req}x")

    # 4. Opening hook: first 3 seconds
    n3 = max(1, int(3 / step))
    sp3 = float(np.mean(speech_out[:n3])) if len(speech_out) else 0.0
    i3 = float(np.mean(interest_out[:n3])) if len(interest_out) else 0.0
    imean = float(np.mean(interest_out)) if len(interest_out) else 1e-6
    a3 = float(np.mean(audio_out_db[:n3])) if len(audio_out_db) else -60
    amed = float(np.median(audio_out_db)) if len(audio_out_db) else -60
    first_word = min((w.start for w in words), default=out_duration)
    hook = 100 * (0.35 * sp3 + 0.4 * clamp01(i3 / max(imean * 1.1, 1e-6)) + 0.25 * clamp01((a3 - amed + 6) / 12))
    if first_word > 1.5:
        hook *= 0.8

    # 5. Retention potential: little dead air, sensible length, payoff not at the very start
    active = float(np.mean((speech_out > 0.5) | (interest_out > 0.3))) if len(interest_out) else 0.0
    d = out_duration
    length_f = 1.0 if 15 <= d <= 40 else (max(0.6, 1 - (d - 40) / 60) if d > 40 else max(0.5, d / 15))
    pos = peak_out / max(d, 1e-6)
    pos_f = 1.0 if 0.2 <= pos <= 0.85 else 0.8
    ret = 100 * clamp01(math.sqrt(active) * length_f * pos_f)

    # 6. Uniqueness: different from this streamer's recent clips, rare chat spike
    fp = set(fingerprint(transcript_text, chat_top))
    sim = max((jaccard(fp, set(h)) for h in history_fps), default=0.0)
    rarity = (sum(1 for r in history_ratios if r < ratio) / len(history_ratios)) if history_ratios else 0.5
    uniq = 100 * (0.7 * (1 - sim) + 0.3 * rarity)
    if sim > 0.5:
        expl.append(f"similar to a recent clip ({sim:.0%} overlap)")
    if history_ratios and rarity >= 0.9:
        expl.append("one of the biggest chat reactions recorded for this streamer")

    comps = {
        "chat_engagement": chat, "emotional_intensity": emo, "entertainment_value": ent,
        "opening_hook": hook, "retention_potential": ret, "uniqueness": uniq,
    }
    wsum = sum(weights.get(k, 0) for k in comps) or 1
    total = sum(comps[k] * weights.get(k, 0) for k in comps) / wsum
    comps = {k: round(float(v), 1) for k, v in comps.items()}
    return round(float(total), 1), comps, expl, sorted(fp)
