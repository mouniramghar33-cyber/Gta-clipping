"""Title, hook, description, hashtags and thumbnail suggestion for a clip.

Default: deterministic, built only from the transcript (real quotes) and measured chat
reactions, so nothing is made up. Optionally an OpenAI-compatible endpoint (e.g. a local
Ollama model) can rewrite them. It gets the same facts and must not add new ones.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

PHRASES = {
    "laugh": ("had chat in tears", "CHAT COULDN'T STOP LAUGHING", "😭"),
    "shock": ("left chat speechless", "CHAT COULDN'T BELIEVE THIS", "😳"),
    "hype": ("made chat go crazy", "CHAT WENT CRAZY", "🔥"),
    "fail": ("got flooded with L's", "CHAT SPAMMED L", "💀"),
    "tension": ("had chat going wild", "CHAT WAS NOT READY FOR THIS", "😬"),
    "clip": ("had chat yelling CLIP IT", "CHAT YELLED CLIP IT", "🎬"),
}

_emoji = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def strip_emoji(t: str) -> str:
    return _emoji.sub("", t).strip()


def best_quote(words, peak: float, max_words: int = 12) -> str | None:
    """A literal sentence from the transcript near the peak (no paraphrasing)."""
    if not words:
        return None
    near = [w for w in words if peak - 6 <= w.start <= peak + 4]
    if len(near) < 2:
        return None
    # split into sentences
    sents, cur = [], []
    for w in near:
        cur.append(w)
        if w.text.endswith((".", "!", "?")):
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)
    def quality(s):
        n = len(s)
        excl = sum(w.text.endswith("!") for w in s)
        dist = min(abs(w.start - peak) for w in s)
        prob = sum(w.prob for w in s) / n
        return (2 <= n <= max_words) * 2 + excl + prob - dist / 4
    s = max(sents, key=quality)
    if not 2 <= len(s) <= max_words or sum(w.prob for w in s) / len(s) < 0.6:
        return None
    q = " ".join(w.text for w in s).strip()
    return q if 6 <= len(q) <= 70 else None


def hashtagify(t: str | None) -> str | None:
    if not t:
        return None
    tag = re.sub(r"[^A-Za-z0-9]", "", t)
    return tag.lower() if 2 <= len(tag) <= 30 else None


def build_metadata(*, streamer: str, display: str | None, category: str | None, top_cat: str | None,
                   words, peak_out: float, score: float, explanation: list[str], src_start: float,
                   src_end: float, peak_wall: float, stream_offset: float | None,
                   default_tags: list[str], credit_template: str, layout_kind: str,
                   llm_base_url: str = "", llm_model: str = "") -> dict:
    name = display or streamer
    phrase, hook_cat, emo = PHRASES.get(top_cat or "", ("had chat going crazy", "CHAT WENT CRAZY", "💀"))
    quote = best_quote(words, peak_out)
    if quote:
        title = f'{name}: "{quote}" {emo}'
        hook = quote.upper() if len(quote) <= 34 else hook_cat
    else:
        title = f"{name} {phrase} {emo}"
        hook = hook_cat
    if category and len(title) + len(category) + 3 <= 95:
        title = f"{title} | {category}"
    credit = credit_template.format(streamer=streamer)
    when = datetime.fromtimestamp(peak_wall, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    desc_lines = [f"{name} live on Twitch" + (f" ({category})" if category else "") + f", {when}.",
                  credit]
    tags = []
    for t in [hashtagify(streamer), hashtagify(category), *default_tags,
              {"laugh": "funny", "shock": "wtf", "hype": "hype", "fail": "fail"}.get(top_cat or "")]:
        if t and t not in tags:
            tags.append(t)
    thumb = (f"Frame at {peak_out:.1f}s (the peak reaction)"
             + (", streamer's face visible in the facecam panel" if layout_kind in ("stack", "track") else "")
             + f"; overlay text: \"{strip_emoji(hook)}\"")
    md = {
        "title": title[:100],
        "hook": strip_emoji(hook),
        "description": "\n".join(desc_lines),
        "hashtags": tags[:6],
        "thumbnail_suggestion": thumb,
        "explanation": f"Score {score:.0f}/100. " + "; ".join(explanation),
        "timestamps": {
            "clip_start_utc": datetime.fromtimestamp(src_start, timezone.utc).isoformat(),
            "clip_end_utc": datetime.fromtimestamp(src_end, timezone.utc).isoformat(),
            "peak_utc": datetime.fromtimestamp(peak_wall, timezone.utc).isoformat(),
            "stream_offset": _hms(stream_offset) if stream_offset is not None else None,
        },
        "quote": quote,
    }
    if llm_base_url and llm_model:
        md.update(_llm_rewrite(llm_base_url, llm_model, md, name, category, words, top_cat))
    return md


def _hms(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _llm_rewrite(base_url: str, model: str, md: dict, name: str, category: str | None, words,
                 top_cat: str | None) -> dict:
    transcript = " ".join(w.text for w in words)[:1500]
    prompt = (
        "Write short-form video metadata for a livestream clip. Use ONLY the facts given; do not "
        "invent events, names or claims. Reply with JSON keys title (<=90 chars), hook (<=34 chars, "
        "no emoji) and description (<=200 chars).\n"
        f"Streamer: {name}\nCategory: {category}\nChat reaction type: {top_cat}\n"
        f"Transcript: {transcript}\nCurrent title: {md['title']}"
    )
    try:
        r = httpx.post(f"{base_url.rstrip('/')}/chat/completions", timeout=60, json={
            "model": model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4, "response_format": {"type": "json_object"},
        })
        r.raise_for_status()
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        out = {}
        if isinstance(data.get("title"), str) and 5 < len(data["title"]) <= 100:
            out["title"] = data["title"]
        if isinstance(data.get("hook"), str) and 3 < len(data["hook"]) <= 40:
            out["hook"] = strip_emoji(data["hook"])
        if isinstance(data.get("description"), str) and len(data["description"]) <= 300:
            out["description"] = data["description"] + "\n" + md["description"].splitlines()[-1]
        return out
    except Exception as e:  # noqa: BLE001 - fall back to deterministic metadata
        log.warning("LLM metadata failed, using deterministic metadata: %s", e)
        return {}
