"""Configuration: defaults in code, overrides in <data_dir>/config.yaml.

Secrets never live here; they come from environment variables (see secrets.py).
The dashboard edits the YAML file through `update()`, so changes persist across restarts.
"""
from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    # Streamers listed here are seeded into the database on first start.
    # After that, manage them from the dashboard.
    "streamers": [],
    "live_check_interval_sec": 60,
    "recording": {
        # Streamlink quality preference, first available wins.
        "quality": "1080p60,1080p,936p60,900p60,720p60,720p,best",
        "segment_sec": 2,
        # Minutes of recorded segments kept as a rolling buffer (more = more disk).
        "buffer_minutes": 15,
        "disable_ads": True,
    },
    "chat": {
        "window_sec": 10,          # rolling analysis window
        "step_sec": 2,             # how often the window advances
        "baseline_minutes": 30,    # history used for the streamer's "normal" activity
        "min_baseline_windows": 18,  # need ~3 min of history before detecting
        # Chat reacts after the event; shift chat timestamps back by this much.
        "reaction_delay_sec": 5,
    },
    "detection": {
        "min_messages": 8,          # ignore windows with fewer messages
        "z_threshold": 3.0,         # robust z-score vs baseline
        "ratio_threshold": 2.2,     # messages vs baseline median
        "hype_share_threshold": 0.35,  # share of messages with reaction tokens
        "cooldown_sec": 20,         # min gap before a new, separate moment
        "merge_gap_sec": 20,        # detections closer than this merge
        "max_moment_sec": 90,       # cap on one merged moment
        # Audio may raise a score but never triggers a clip on its own.
        "audio_boost_db": 8.0,
    },
    "clip": {
        "pre_sec": 30,
        "post_sec": 30,
        "min_sec": 15,
        "max_sec": 60,
        "remove_silence": True,
        "min_silence_sec": 1.2,     # remove dead air longer than this
        "keep_silence_sec": 0.3,    # keep this much of a removed gap
    },
    "scoring": {
        "weights": {
            "chat_engagement": 25,
            "emotional_intensity": 15,
            "entertainment_value": 20,
            "opening_hook": 15,
            "retention_potential": 15,
            "uniqueness": 10,
        },
        "priority_threshold": 80,
        "min_score_to_keep": 35,    # clips below this are rendered but auto-rejected
    },
    "whisper": {
        "model": "small",           # tiny/base/small/medium/large-v3 (CPU: small is a good balance)
        "compute_type": "int8",
        "device": "cpu",
        "language": None,           # None = automatic language detection
        "beam_size": 5,
    },
    "render": {
        "width": 1080,
        "height": 1920,
        "fps": 60,
        "crf": 20,
        "preset": "veryfast",
        "audio_bitrate": "192k",
        "loudness_lufs": -14,
        "workers": 1,               # parallel clip renders (CPU heavy)
        "burn_hook": True,          # show the hook text for the first seconds
        "default_style": "clean",   # style of the preview render (accounts can use others)
        "hook_sec": 2.5,
    },
    "subtitles": {
        "font": "Montserrat ExtraBold",
        "fallback_font": "DejaVu Sans",
        "font_size": 82,
        "outline": 6,
        "max_chars_per_line": 18,
        "max_lines": 2,
        "uppercase": False,
        "highlight_color": "#FFE600",
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        # Platform UI safe zone as fractions of the frame height/width.
        "safe_top": 0.10,
        "safe_bottom": 0.22,
        "safe_side": 0.08,
    },
    "metadata": {
        "default_hashtags": ["twitch", "streamer", "clips"],
        # Optional OpenAI-compatible endpoint (e.g. a local Ollama server) for titles.
        # Leave empty to use deterministic, transcript-based metadata.
        "llm_base_url": "",
        "llm_model": "",
    },
    "publishing": {
        # Global safety switches (also on the dashboard header).
        "dry_run": True,            # everything except the actual upload; turn off when ready
        "emergency_stop": False,    # stops all uploads immediately
        # Defaults for new accounts (each account can override them).
        "min_score": 80,
        "daily_limit": 3,
        "min_interval_min": 120,
        "posting_times": ["12:30", "16:30", "19:30", "22:00"],
        "timezone": "Europe/Amsterdam",
        "max_attempts": 6,
        "youtube_daily_uploads": 100,   # API project quota for videos.insert (default bucket)
        "youtube_privacy": "public",
        "youtube_category": "20",       # Gaming
        "credit_template": "🎥 twitch.tv/{streamer}",
        "require_rights": True,         # auto-publish only streamers marked "rights granted"
        "public_link_days": 7,          # lifetime of the private link providers download from
    },
    "styles": {
        "clean": {"label": "Clean", "hook": True, "caption_animation": "pop", "font_size": 82,
                  "uppercase": False, "highlight_color": "#FFE600", "zoom_peak": 0.0,
                  "jumpcut_zoom": False, "sfx": False, "layout": "auto"},
        "energetic": {"label": "Energetic", "hook": True, "caption_animation": "pop", "font_size": 88,
                      "uppercase": True, "highlight_color": "#FFE600", "zoom_peak": 0.14,
                      "jumpcut_zoom": True, "sfx": True, "sfx_volume": 0.35, "layout": "auto"},
        "minimal": {"label": "Minimal", "hook": False, "caption_animation": "none", "font_size": 72,
                    "uppercase": False, "highlight_color": "#FFFFFF", "zoom_peak": 0.0,
                    "jumpcut_zoom": False, "sfx": False, "layout": "auto"},
    },
    "notifications": {
        "telegram": True,           # needs TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
        "events": {"upload_success": True, "upload_failed": True, "viral_clip": True,
                   "account_problem": True, "approval_needed": True, "daily_summary": True},
        "viral_score": 85,
        "daily_summary_hour": 9,
    },
    "analytics": {
        "enabled": True,
        "interval_hours": 3,
        "max_age_days": 30,
        "learning": True,           # use results to adjust streamer priority, style and posting times
        "min_samples": 3,
        "explore_rate": 0.2,
    },
    "storage": {
        "max_storage_gb": 50,
        "rejected_retention_days": 3,
        "master_retention_days": 3,
        "clip_retention_days": 30,
        "cleanup_interval_min": 10,
    },
    "dashboard": {"host": "0.0.0.0", "port": 8080},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Thread-safe config with persistence. Read with cfg["a"]["b"] or cfg.get("a.b")."""

    def __init__(self, data_dir: str | os.PathLike):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "config.yaml"
        self._lock = threading.RLock()
        self._overrides: dict = {}
        if self.path.exists():
            self._overrides = yaml.safe_load(self.path.read_text()) or {}
        self._data = _deep_merge(DEFAULTS, self._overrides)

    def reload(self) -> None:
        """Re-read config.yaml if another process (dashboard) changed it."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != getattr(self, "_mtime", None):
            with self._lock:
                self._overrides = yaml.safe_load(self.path.read_text()) or {}
                self._data = _deep_merge(DEFAULTS, self._overrides)
                self._mtime = mtime

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._data[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node)

    def as_dict(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch: dict) -> dict:
        """Merge `patch` into the overrides, validate and persist."""
        with self._lock:
            new_over = _deep_merge(self._overrides, patch)
            merged = _deep_merge(DEFAULTS, new_over)
            validate(merged)
            self._overrides = new_over
            self._data = merged
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(self._overrides, sort_keys=False, allow_unicode=True))
            tmp.replace(self.path)
            return self.as_dict()

    # convenience paths
    @property
    def segments_dir(self) -> Path:
        return self.data_dir / "segments"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autoclipper.db"


def validate(c: dict) -> None:
    w = c["scoring"]["weights"]
    if any((not isinstance(v, (int, float))) or v < 0 for v in w.values()) or sum(w.values()) <= 0:
        raise ValueError("scoring weights must be non-negative numbers with a positive sum")
    p = c["publishing"]
    if not 0 <= p["min_score"] <= 100:
        raise ValueError("publishing.min_score must be 0-100")
    if p["daily_limit"] < 0 or p["min_interval_min"] < 0 or p["max_attempts"] < 1:
        raise ValueError("publishing limits must be positive")
    for t in p["posting_times"]:
        hh, _, mm = str(t).partition(":")
        if not (hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
            raise ValueError(f"invalid posting time {t!r} (use HH:MM)")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(p["timezone"])
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"unknown timezone {p['timezone']!r}") from e
    if not c["styles"]:
        raise ValueError("at least one style is required")
    cl = c["clip"]
    if not (5 <= cl["min_sec"] < cl["max_sec"] <= 180):
        raise ValueError("clip.min_sec/max_sec out of range")
    if c["chat"]["window_sec"] < 2 or c["chat"]["step_sec"] < 1:
        raise ValueError("chat window/step too small")
    for k in ("priority_threshold", "min_score_to_keep"):
        if not 0 <= c["scoring"][k] <= 100:
            raise ValueError(f"scoring.{k} must be 0-100")
