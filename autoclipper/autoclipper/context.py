"""Context understanding for a candidate clip.

Combines what was SAID (Whisper transcript), how CHAT reacted (reaction categories, repeated
messages, mentions), how the streamer LOOKED (facial expression, body movement), how it
SOUNDED (audio peaks) and WHERE it happened (stream category/title, GTA VI, collaborations)
into moment labels and an "is this actually entertaining?" decision.

It's a transparent rule-based classifier: every label lists the evidence it came from. An
optional local LLM (OpenAI-compatible endpoint, e.g. Ollama) can add a second opinion, but it is
never required.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

GTA_CATEGORIES = ("grand theft auto vi", "gta vi", "gta 6", "grand theft auto 6")

# Transcript cues (lowercase regex) -> label
SPEECH = {
    "funny": r"\b(ha(ha)+|lmao|i'?m (dead|crying)|that'?s (so )?funny|bro what)\b",
    "rage": r"\b(f+u+c+k+|what the f|are you (serious|kidding)|i'?m (so )?(mad|tilted)|this game is|bull ?shit|stupid)\b",
    "argument": r"\b(shut up|no you|you('re| are) (lying|wrong)|stop (talking|lying)|bro chill|why would you)\b",
    "reaction": r"\b(no way|oh my god|what\?*!|wait what|yo+|holy|no+ shot|are you serious)\b",
    "emotional": r"\b(i love (you|y'all)|thank you (so much|guys)|i'?m (so )?(proud|grateful)|this means|crying)\b",
    "donation": r"\b(\d+ ?(dollars|bucks|gifted|subs|bits)|thank you for the|donat(ed|ion)|super ?chat|gifted)\b",
    "embarrassing": r"\b(that'?s (so )?embarrassing|i'?m embarrassed|don'?t clip that|delete that|oh no)\b",
    "fail": r"\b(i (died|lost)|we lost|i'?m dead|how did i (miss|die)|that'?s (so )?bad|unlucky)\b",
    "impressive": r"\b(let'?s go+|clean|insane|that was (crazy|insane)|did you see that|first try|goat)\b",
    "discovery": r"\b(look at (this|that)|you can (go|do)|did you know|found (a|the|it)|secret|hidden)\b",
    "controversial": r"\b(i don'?t care what|unpopular opinion|i'?m (just )?saying|cancel(led)?|beef)\b",
}
GTA = {
    "first_reaction": r"\b(first time|it'?s finally (here|out)|we'?re in|it'?s out|launch)\b",
    "npc": r"\b(npc|this (guy|lady|man|woman) (just|is)|why is he|random (guy|person))\b",
    "police_chase": r"\b(cops?|police|wanted|stars?|chase|swat|helicopter|run+ ?!?)\b",
    "hidden_location": r"\b(hidden|secret (place|area|room|location)|how do (we|i) get (in|up)|look where)\b",
    "easter_egg": r"\b(easter egg|reference|bigfoot|alien|ufo|ghost)\b",
    "realistic": r"\b(so (real|realistic)|looks real|the detail|physics|graphics are)\b",
    "glitch": r"\b(glitch|bug(ged)?|broken|clipping|stuck)\b",
    "secret_mission": r"\b(secret mission|side mission|stranger|mystery)\b",
    "death": r"\b(wasted|i died|we died|he killed me|dead)\b",
    "graphics": r"\b(graphics|4k|ray ?tracing|gorgeous|beautiful|insane looking)\b",
    "online": r"\b(gta online|online|lobby|crew)\b",
    "vice_city": r"\b(vice city|leonida|jason|lucia)\b",
}
CHAT_TO_LABEL = {"laugh": "funny", "shock": "reaction", "hype": "impressive", "fail": "fail",
                 "tension": "argument", "clip": "meme"}


@dataclass
class ContextResult:
    labels: dict = field(default_factory=dict)      # label -> confidence 0..1
    evidence: dict = field(default_factory=dict)    # label -> [reasons]
    gta: bool = False
    gta_tags: list = field(default_factory=list)
    collab: list = field(default_factory=list)      # other watchlist streamer ids
    entertaining: bool = True
    reason: str = ""
    primary: str = "reaction"

    def as_dict(self) -> dict:
        return {"labels": {k: round(v, 2) for k, v in sorted(self.labels.items(), key=lambda x: -x[1])},
                "evidence": self.evidence, "gta": self.gta, "gta_tags": self.gta_tags, "collab": self.collab,
                "entertaining": self.entertaining, "reason": self.reason, "primary": self.primary}


def is_gta(category: str | None, title: str | None = None) -> bool:
    c = (category or "").lower()
    return any(g in c for g in GTA_CATEGORIES)


def mentions(text: str, names: dict[str, str], exclude: str) -> set[str]:
    t = " " + re.sub(r"[^a-z0-9@ ]", " ", text.lower()) + " "
    found = set()
    for alias, sid in names.items():
        if sid != exclude and (f" {alias} " in t or f"@{alias} " in t):
            found.add(sid)
    return found


def classify(*, streamer: str, transcript: str, chat_texts: list[str], chat_cats: dict, chat_msgs: int,
             sig: dict, emotions: dict, category: str | None, title: str | None,
             names: dict[str, str], peak_ratio: float) -> ContextResult:
    r = ContextResult()
    low = transcript.lower()

    def add(label, conf, why):
        r.labels[label] = min(1.0, r.labels.get(label, 0) + conf)
        r.evidence.setdefault(label, []).append(why)

    for label, rx in SPEECH.items():
        hits = len(re.findall(rx, low))
        if hits:
            add(label, min(0.6, 0.3 * hits), f"said: {hits}x {label} cue")
    for cat, n in (chat_cats or {}).items():
        share = n / max(chat_msgs, 1)
        if cat in CHAT_TO_LABEL and share >= 0.06:
            add(CHAT_TO_LABEL[cat], min(0.7, share * 2), f"chat: {share:.0%} {cat}")
    if sig.get("max_repetition", 0) >= 0.2:
        add("meme", 0.3, f"chat spammed one message ({sig['max_repetition']:.0%})")
    # face / body
    if emotions.get("happy", 0) >= 0.4:
        add("funny", 0.35, f"streamer laughing/smiling ({emotions['happy']:.0%})")
    if emotions.get("surprise", 0) >= 0.3:
        add("reaction", 0.35, f"surprised face ({emotions['surprise']:.0%})")
    if emotions.get("anger", 0) >= 0.3:
        add("rage", 0.4, f"angry face ({emotions['anger']:.0%})")
    if emotions.get("sad", 0) >= 0.45:
        add("emotional", 0.3, f"sad/emotional face ({emotions['sad']:.0%})")
    if sig.get("max_body_spike", 0) >= 2.0:
        add("reaction", 0.2, f"big body movement ({sig['max_body_spike']:.1f}x normal)")
    if sig.get("max_audio_spike_db", 0) >= 12:
        add("reaction", 0.15, f"loud reaction (+{sig['max_audio_spike_db']:.0f} dB)")
        if "rage" in r.labels:
            add("rage", 0.15, "shouting")
    # collaborations: other watchlist streamers named in title, speech or (repeatedly) in chat
    collab = mentions(f"{title or ''} {transcript}", names, streamer)
    chat_mentions: dict[str, int] = {}
    for t in chat_texts:
        for sid in mentions(t, names, streamer):
            chat_mentions[sid] = chat_mentions.get(sid, 0) + 1
    collab |= {sid for sid, n in chat_mentions.items() if n >= max(3, 0.05 * len(chat_texts))}
    if collab:
        r.collab = sorted(collab)
        add("collab", 0.5, "with " + ", ".join(r.collab))
    # GTA VI
    if is_gta(category, title):
        r.gta = True
        text = low + " " + " ".join(chat_texts).lower()
        for tag, rx in GTA.items():
            if re.search(rx, text):
                r.gta_tags.append(tag)
    # decision: ordinary conversation vs entertaining
    strongest = max(r.labels.values(), default=0)
    if not r.labels:
        r.entertaining = peak_ratio >= 3.0  # chat clearly exploded even if we can't name why
        r.reason = "no entertaining cues found" if not r.entertaining else "chat exploded (unlabelled)"
        r.primary = "reaction"
    else:
        r.primary = max(r.labels, key=r.labels.get)
        r.entertaining = strongest >= 0.3 or peak_ratio >= 3.0
        r.reason = "; ".join(r.evidence[r.primary][:2]) if r.entertaining else "only weak cues (ordinary conversation)"
    return r


def llm_opinion(base_url: str, model: str, transcript: str, chat_sample: list[str], category: str | None,
                labels: dict) -> dict:
    """Optional second opinion from a local LLM. Returns {} on any problem."""
    prompt = ("You judge livestream moments for short-form clips. Based ONLY on this data, answer JSON "
              '{"entertaining": true/false, "labels": [..], "summary": "<=20 words"}. Allowed labels: funny, rage, '
              "argument, reaction, collab, emotional, controversial, donation, embarrassing, fail, impressive, "
              "discovery, meme. Do not invent events.\n"
              f"Category: {category}\nTranscript: {transcript[:1200]}\nChat sample: {' | '.join(chat_sample[:40])[:800]}\n"
              f"Signals: {json.dumps(labels)}")
    try:
        r = httpx.post(f"{base_url.rstrip('/')}/chat/completions", timeout=45, json={
            "model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0,
            "response_format": {"type": "json_object"}})
        r.raise_for_status()
        d = json.loads(r.json()["choices"][0]["message"]["content"])
        return d if isinstance(d, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("LLM context opinion failed: %s", e)
        return {}
