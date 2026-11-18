"""FFmpeg helpers: extract a source window from segments, render the vertical clip."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .layout import LayoutPlan, probe_size
from .recorder import Segment

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    pass


def run_ffmpeg(args: list[str], timeout: int = 1800) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise FFmpegError(f"ffmpeg failed ({p.returncode}): {p.stderr[-1500:]}")


def extract_window(segs: list[Segment], out: Path) -> list[tuple[float, float, float]]:
    """Concatenate segments losslessly. Returns mapping [(wall_start, wall_end, media_start)]."""
    if not segs:
        raise ValueError("no segments to extract")
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{s.path}'\n" for s in segs))
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy", str(out)])
    mapping, acc = [], 0.0
    for s in segs:
        mapping.append((s.start, s.end, acc))
        acc += s.dur
    return mapping


def wall_to_media(t: float, mapping: list[tuple[float, float, float]]) -> float:
    for ws, we, ms in mapping:
        if t < ws:
            return ms
        if ws <= t <= we:
            return ms + (t - ws)
    ws, we, ms = mapping[-1]
    return ms + (we - ws)


def media_to_wall(t: float, mapping: list[tuple[float, float, float]]) -> float:
    for ws, we, ms in mapping:
        if ms <= t <= ms + (we - ws):
            return ws + (t - ms)
    ws, we, ms = mapping[-1]
    return we


def extract_audio(src: Path, out: Path) -> None:
    run_ffmpeg(["-i", str(src), "-map", "0:a:0", "-ac", "1", "-ar", "16000", str(out)])


def _sendcmd_file(plan: LayoutPlan, keep_start: float, path: Path) -> None:
    # keyframes are relative to the start of the analysed range (= first keep interval)
    lines = []
    pts = plan.track_x
    for (t0, x0), (t1, x1) in zip(pts, pts[1:] + [pts[-1]]):
        steps = max(1, int((t1 - t0) / 0.1)) if t1 > t0 else 1
        for k in range(steps):
            t = t0 + (t1 - t0) * k / steps if t1 > t0 else t0
            x = x0 + (x1 - x0) * k / steps
            lines.append(f"{keep_start + t:.2f} crop@trk x {int(x) // 2 * 2};")
    path.write_text("\n".join(lines) + "\n")


def zoom_expr(keep: list[tuple[float, float]], peak_out: float | None, peak_zoom: float,
              jumpcut: bool) -> str | None:
    """Per-frame zoom factor (output time t): a short punch-in on the peak, and an alternating
    6% zoom on every cut made by dead-air removal (a classic jump-cut treatment)."""
    terms = []
    if peak_zoom > 0 and peak_out is not None:
        p = peak_out
        terms.append(f"{peak_zoom:.3f}*clip((t-{p - 0.35:.3f})/0.2,0,1)*clip(({p + 1.9:.3f}-t)/0.45,0,1)")
    if jumpcut and len(keep) > 1:
        acc = 0.0
        for i, (a, b) in enumerate(keep):
            d = b - a
            if i % 2 == 1:
                terms.append(f"0.06*between(t,{acc:.3f},{acc + d:.3f})")
            acc += d
    return "1+" + "+".join(terms) if terms else None


def render_clip(src: Path, keep: list[tuple[float, float]], plan: LayoutPlan, ass_path: Path,
                out: Path, rcfg: dict, work: Path, fonts_dir: str | None = None,
                style: dict | None = None, peak_out: float | None = None,
                sfx_at: float | None = None) -> None:
    style = style or {}
    W, H = rcfg["width"], rcfg["height"]
    _, _, src_fps = probe_size(str(src))
    fps = min(int(rcfg["fps"]), int(round(src_fps)) or 30)
    k = len(keep)
    f: list[str] = []
    pre = f"[0:v]fps={fps}"
    if plan.kind == "track":
        cmd_file = work / "track.cmd"
        _sendcmd_file(plan, keep[0][0], cmd_file)
        c = plan.content
        pre += (f",sendcmd=f='{cmd_file}',crop@trk=w={plan.track_w}:h={int(c.h)//2*2}"
                f":x={int(plan.track_x[0][1])//2*2}:y={int(c.y)//2*2}")
    f.append(f"{pre},split={k}" + "".join(f"[vs{i}]" for i in range(k)))
    f.append(f"[0:a]aresample=48000,asplit={k}" + "".join(f"[as{i}]" for i in range(k)))
    for i, (a, b) in enumerate(keep):
        f.append(f"[vs{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        fades = ""
        if k > 1 and b - a > 0.1:  # tiny fades so cuts don't click
            fades = f",afade=t=in:d=0.015,afade=t=out:st={b - a - 0.015:.3f}:d=0.015"
        f.append(f"[as{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS{fades}[a{i}]")
    f.append("".join(f"[v{i}][a{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=1[vc][ac]")

    if plan.kind == "stack":
        cam, g = plan.cam, plan.game
        top_h, bot_h = plan.top_h, H - plan.top_h
        f.append("[vc]split[c1][c2]")
        f.append(f"[c1]crop={int(cam.w)}:{int(cam.h)}:{int(cam.x)}:{int(cam.y)},scale={W}:{top_h}:flags=lanczos[top]")
        f.append(f"[c2]crop={int(g.w)}:{int(g.h)}:{int(g.x)}:{int(g.y)},scale={W}:{bot_h}:flags=lanczos[bot]")
        f.append("[top][bot]vstack[v0]")
    elif plan.kind == "fit":
        c, g = plan.content, plan.game
        gh = int(round(W * g.h / g.w / 2) * 2)
        f.append("[vc]split[c1][c2]")
        f.append(f"[c1]crop={int(c.w)//2*2}:{int(c.h)//2*2}:{int(c.x)}:{int(c.y)},scale={W//4}:{H//4}:"
                 f"force_original_aspect_ratio=increase,crop={W//4}:{H//4},boxblur=12:2,"
                 f"eq=brightness=-0.12:saturation=1.1,scale={W}:{H}[bg]")
        f.append(f"[c2]crop={int(g.w)}:{int(g.h)}:{int(g.x)}:{int(g.y)},scale={W}:{gh}:flags=lanczos[fg]")
        f.append("[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")
    else:  # track
        f.append(f"[vc]scale={W}:{H}:flags=lanczos,setsar=1[v0]")

    ass = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    z = zoom_expr(keep, peak_out, float(style.get("zoom_peak", 0) or 0), bool(style.get("jumpcut_zoom")))
    if z:
        f.append(f"[v0]scale=w='trunc({W}*({z})/2)*2':h='trunc({H}*({z})/2)*2':eval=frame:flags=bicubic,"
                 f"crop={W}:{H}:'(iw-{W})/2':'(ih-{H})/2'[vz]")
        vin = "[vz]"
    else:
        vin = "[v0]"
    sub = f"subtitles='{ass}'" + (f":fontsdir='{fonts_dir}'" if fonts_dir else "")
    f.append(f"{vin}{sub},format=yuv420p[v]")
    inputs = ["-i", str(src)]
    if style.get("sfx") and sfx_at is not None:
        # procedurally generated whoosh (no third-party sound files / licences needed)
        inputs += ["-f", "lavfi", "-i", "anoisesrc=d=0.6:c=pink:a=0.6:r=48000"]
        vol = float(style.get("sfx_volume", 0.35))
        ms = max(0, int((sfx_at - 0.35) * 1000))
        f.append(f"[1:a]highpass=f=500,lowpass=f=7000,afade=t=in:d=0.3,afade=t=out:st=0.3:d=0.3,"
                 f"volume={vol:.2f},adelay={ms}|{ms},aformat=channel_layouts=stereo[sfx]")
        f.append("[ac]aformat=channel_layouts=stereo[acs];[acs][sfx]amix=inputs=2:duration=first:normalize=0[am]")
        ain = "[am]"
    else:
        ain = "[ac]"
    f.append(f"{ain}loudnorm=I={rcfg['loudness_lufs']}:TP=-1.5:LRA=11,aresample=48000[a]")
    run_ffmpeg([
        *inputs, "-filter_complex", ";".join(f), "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", rcfg["preset"], "-crf", str(rcfg["crf"]),
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", rcfg["audio_bitrate"], "-ar", "48000",
        "-movflags", "+faststart", str(out),
    ])


def measure_lufs(path: Path) -> float | None:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
                        "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    for line in reversed(p.stderr.splitlines()):
        line = line.strip()
        if line.startswith("I:") and "LUFS" in line:
            return float(line.split()[1])
    return None


def correct_loudness(path: Path, target: float, tolerance: float = 0.7) -> float | None:
    """Single-pass loudnorm undershoots on short clips; fix the remaining gap with a gain
    change plus a true-peak limiter. Only audio is re-encoded."""
    lufs = measure_lufs(path)
    if lufs is None or abs(lufs - target) <= tolerance:
        return lufs
    gain = max(-12.0, min(12.0, target - lufs))
    tmp = path.with_suffix(".tmp.mp4")
    run_ffmpeg(["-i", str(path), "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                "-af", f"volume={gain:.2f}dB,alimiter=limit=0.84:level=false", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(tmp)])
    tmp.replace(path)
    return measure_lufs(path)


def make_thumbnail(video: Path, t: float, out: Path) -> None:
    run_ffmpeg(["-ss", f"{max(0.0, t):.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)])


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, timeout=30).stdout.strip()
    return float(out or 0)
