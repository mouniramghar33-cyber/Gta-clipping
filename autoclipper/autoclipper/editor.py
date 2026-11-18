"""Clip masters and style variants.

A master is everything needed to re-edit a clip without the live recording: the lossless
source window (src.ts) plus the edit decisions (spec.json): cut points, peak, layout, words,
highlighted words, hook. Any style can then be rendered on demand, for example a
different look for each social account.
"""
from __future__ import annotations

import copy
import json
import logging
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from . import render, subtitles
from .layout import Box, LayoutPlan, _plan_fit
from .transcribe import Word

log = logging.getLogger(__name__)


def layout_to_dict(p: LayoutPlan) -> dict:
    return asdict(p)


def layout_from_dict(d: dict) -> LayoutPlan:
    d = dict(d)
    for k in ("content", "face", "cam", "game", "face_out", "game_out", "cam_rect"):
        if d.get(k) is not None:
            d[k] = Box(**d[k])
    d["track_x"] = [tuple(x) for x in d.get("track_x") or []]
    return LayoutPlan(**d)


def save_master(master_dir: Path, src: Path, spec: dict) -> None:
    master_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), master_dir / "src.ts")
    (master_dir / "spec.json").write_text(json.dumps(spec))


def load_spec(master_dir: Path) -> dict:
    return json.loads((Path(master_dir) / "spec.json").read_text())


def style_for(cfg, name: str | None) -> tuple[str, dict]:
    styles = cfg.get("styles") or {}
    if not name or name not in styles:
        name = "clean" if "clean" in styles else next(iter(styles))
    return name, styles[name]


def render_style(cfg, master_dir: Path, style_name: str, out: Path, work: Path) -> Path:
    """Render one styled variant of a clip from its master."""
    spec = load_spec(master_dir)
    name, style = style_for(cfg, style_name)
    rc = cfg["render"]
    sc = copy.deepcopy(cfg["subtitles"])
    for k in ("font_size", "uppercase", "highlight_color"):
        if k in style:
            sc[k] = style[k]
    layout = layout_from_dict(spec["layout"])
    if style.get("layout") == "fit" and layout.kind != "fit":
        _plan_fit(layout, rc["width"], rc["height"])
    keep = [tuple(x) for x in spec["keep"]]
    words = [Word(**w) for w in spec["words"]]
    place = subtitles.choose_placement(rc["width"], rc["height"], layout.kind, layout.face_out,
                                       layout.game_out, layout.top_h, sc)
    hook = spec.get("hook") if style.get("hook", True) and rc.get("burn_hook", True) else None
    ass = subtitles.build_ass(words, keep, set(spec["important"]), place, sc, rc["width"], rc["height"],
                              hook_text=hook, hook_sec=rc.get("hook_sec", 2.5),
                              animation=style.get("caption_animation", "none"))
    work.mkdir(parents=True, exist_ok=True)
    ass_path = work / f"subs_{name}.ass"
    ass_path.write_text(ass, encoding="utf-8")
    # sound effect only on strong, reactive moments (selective, never on every clip)
    sfx_at = spec["peak_out"] if spec.get("top_cat") in ("laugh", "shock", "hype", "fail") \
        and spec.get("score", 0) >= 60 else None
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    render.render_clip(Path(master_dir) / "src.ts", keep, layout, ass_path, out, rc, work,
                       fonts_dir="/usr/share/fonts", style=style, peak_out=spec["peak_out"], sfx_at=sfx_at)
    render.correct_loudness(out, float(rc["loudness_lufs"]))
    log.info("rendered style %s -> %s in %.0fs", name, out.name, time.time() - t0)
    return out


def ensure_variant(cfg, db, clip: dict, style_name: str) -> Path:
    """Path of the clip rendered in `style_name`, rendering it from the master if needed."""
    name, _ = style_for(cfg, style_name)
    row = db.one("SELECT path FROM clip_variants WHERE clip_id=? AND style=?", (clip["id"], name))
    if row and Path(row["path"]).exists():
        return Path(row["path"])
    if not clip.get("master_dir") or not (Path(clip["master_dir"]) / "src.ts").exists():
        # no master (older clip or cleaned up): fall back to the default render
        if clip.get("path") and Path(clip["path"]).exists():
            return Path(clip["path"])
        raise FileNotFoundError("clip master and video are gone")
    base = Path(clip["path"]) if clip.get("path") else Path(clip["master_dir"]) / "x.mp4"
    out = base.with_name(f"{base.stem.split('__')[0]}__{name}.mp4")
    render_style(cfg, Path(clip["master_dir"]), name, out, cfg.work_dir / f"v{clip['id']}_{name}")
    shutil.rmtree(cfg.work_dir / f"v{clip['id']}_{name}", ignore_errors=True)
    db.execute("INSERT OR REPLACE INTO clip_variants (clip_id, style, path, created_at) VALUES (?,?,?,?)",
               (clip["id"], name, str(out), time.time()))
    return out
