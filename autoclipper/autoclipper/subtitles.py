"""Word-by-word ASS subtitles: bold white text, black outline, important words in yellow.

Each caption chunk is drawn at its final layout from the start, with words that haven't
been spoken yet made fully transparent. Words then appear exactly when they're said,
and the text never jumps around.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass

from .cut import map_time
from .layout import Box
from .transcribe import Word

EMPHASIS = {
    "no", "what", "wait", "bro", "why", "how", "never", "dead", "insane", "crazy", "stop", "run",
    "yes", "omg", "wtf", "nah", "holy", "dude", "help", "god", "go", "win", "won", "lost", "lose",
    "best", "worst", "first", "last", "biggest", "huge", "impossible", "unbelievable", "actually",
    "literally", "seriously", "really", "chat", "clip", "money", "million", "dollars", "killed",
    "died", "die", "crash", "police", "cops", "jail", "broke", "rich", "free", "secret", "hidden",
}


@dataclass
class Placement:
    y: int          # vertical centre of subtitles (output px)
    hook_y: int


def ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def font_available(name: str) -> bool:
    if not shutil.which("fc-list"):
        return False
    out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
    return any(name.lower() in line.lower() for line in out.splitlines())


def important_words(words: list[Word], chat_tokens: set[str], loud_times: list[float]) -> set[int]:
    """Indices of words worth highlighting: strong words, numbers, words echoed in chat, shouted words."""
    scored = []
    for i, w in enumerate(words):
        t = re.sub(r"[^\w']", "", w.text).lower()
        if not t:
            continue
        s = 0.0
        if t in EMPHASIS:
            s += 2
        if t.isdigit() or re.match(r"^\d", t):
            s += 2
        if len(t) > 2 and t in chat_tokens:
            s += 2
        if w.text.strip(".,!?").isupper() and len(t) > 1:
            s += 1.5
        if w.text.endswith("!"):
            s += 1
        if any(w.start - 0.25 <= lt <= w.end + 0.25 for lt in loud_times):
            s += 1.5
        if len(t) >= 7:
            s += 0.5
        if s >= 2:
            scored.append((i, s))
    return {i for i, _ in scored}


def _chunks(words: list[tuple[float, float, str, bool]], max_chars: int, max_lines: int):
    """Group words into caption chunks of at most `max_lines` lines."""
    chunk: list = []
    lines: list[list] = [[]]
    def line_len(l):
        return sum(len(x[2]) for x in l) + max(0, len(l) - 1)
    for i, w in enumerate(words):
        pause = chunk and w[0] - chunk[-1][1] > 0.6
        too_long = chunk and w[1] - chunk[0][0] > 3.2
        if pause or too_long:
            yield chunk, lines
            chunk, lines = [], [[]]
        if lines[-1] and line_len(lines[-1]) + 1 + len(w[2]) > max_chars:
            if len(lines) >= max_lines:
                yield chunk, lines
                chunk, lines = [], [[]]
            else:
                lines.append([])
        lines[-1].append(w)
        chunk.append(w)
        if w[2].endswith((".", "!", "?")) and line_len(lines[-1]) > max_chars * 0.5:
            yield chunk, lines
            chunk, lines = [], [[]]
    if chunk:
        yield chunk, lines


def choose_placement(out_w: int, out_h: int, layout_kind: str, face_out: Box | None,
                     game_out: Box | None, top_h: int, sub_cfg: dict) -> Placement:
    """Pick a subtitle position inside the platform safe zone that avoids the face."""
    safe_top = out_h * sub_cfg["safe_top"]
    safe_bot = out_h * (1 - sub_cfg["safe_bottom"])
    box_h = sub_cfg["font_size"] * 2.6
    if layout_kind == "fit" and game_out is not None:
        # below the gameplay, on the blurred background: covers nothing important
        below = game_out.y + game_out.h + box_h / 2 + 40
        cands = [below, game_out.y - box_h / 2 - 40, out_h * 0.70]
    elif layout_kind == "stack":
        cands = [top_h + out_h * 0.30, top_h + out_h * 0.10, out_h * 0.70, top_h - box_h / 2]
    else:
        cands = [out_h * 0.70, out_h * 0.62, out_h * 0.30, out_h * 0.22]
    def ok(y):
        if y - box_h / 2 < safe_top or y + box_h / 2 > safe_bot:
            return False
        if face_out is not None:
            fy0, fy1 = face_out.y - face_out.h * 0.3, face_out.y + face_out.h * 1.3
            if not (y + box_h / 2 < fy0 or y - box_h / 2 > fy1):
                return False
        return True
    y = next((c for c in cands if ok(c)), min(max(out_h * 0.7, safe_top + box_h / 2), safe_bot - box_h / 2))
    hook_c = [safe_top + 110, out_h * 0.2]
    if layout_kind == "stack":
        hook_c = [top_h, top_h + 140]   # on the seam between facecam and gameplay
    if layout_kind == "fit" and game_out is not None:
        hook_c.insert(0, game_out.y - 140)
    hook_y = next((c for c in hook_c if c - 80 >= safe_top and abs(c - y) > box_h and
                   (face_out is None or c + 80 < face_out.y - face_out.h * 0.3 or c - 80 > face_out.y + face_out.h * 1.3)),
                  safe_top + 110)
    return Placement(int(y), int(hook_y))


def build_ass(words: list[Word], keep: list[tuple[float, float]], important: set[int],
              placement: Placement, sub_cfg: dict, out_w: int, out_h: int,
              hook_text: str | None = None, hook_sec: float = 2.5, animation: str = "none") -> str:
    font = sub_cfg["font"] if font_available(sub_cfg["font"]) else sub_cfg["fallback_font"]
    white = ass_color(sub_cfg["text_color"])
    yellow = ass_color(sub_cfg["highlight_color"])
    black = ass_color(sub_cfg["outline_color"])
    fs, ol = sub_cfg["font_size"], sub_cfg["outline"]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {out_w}
PlayResY: {out_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},{fs},{white},{white},{black},&H80000000,1,0,0,0,100,100,0,0,1,{ol},2,5,40,40,0,1
Style: Hook,{font},{int(fs * 0.82)},{white},{white},&H00000000,&H00000000,1,0,0,0,100,100,0,0,3,14,0,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    # map words into output time; drop words inside removed gaps
    mapped = []
    for i, w in enumerate(words):
        a, b = map_time(w.start, keep), map_time(min(w.end, w.start + 1.5), keep)
        if a is None:
            continue
        if b is None or b <= a:
            b = a + max(0.08, min(w.end - w.start, 0.6))
        text = w.text.strip()
        if sub_cfg.get("uppercase"):
            text = text.upper()
        mapped.append((a, b, text, i in important))
    events = []
    x = out_w // 2
    for chunk, lines in _chunks(mapped, sub_cfg["max_chars_per_line"], sub_cfg["max_lines"]):
        c_end = chunk[-1][1] + 0.25
        for k, w in enumerate(chunk):
            t0 = w[0]
            t1 = chunk[k + 1][0] if k + 1 < len(chunk) else c_end
            if t1 <= t0:
                continue
            spoken = {id(x_) for x_ in chunk[: k + 1]}
            parts = []
            for line in lines:
                toks = []
                for wd in line:
                    col = yellow if wd[3] else white
                    alpha = "&H00&" if id(wd) in spoken else "&HFF&"
                    txt = _escape(wd[2])
                    pop = ""
                    if animation == "pop" and wd is w:  # the word being spoken right now pops in
                        pop = "\\fscx122\\fscy122\\t(0,110,\\fscx100\\fscy100)"
                    toks.append(f"{{\\1c{col}\\alpha{alpha}{pop}}}{txt}{{\\fscx100\\fscy100}}")
                parts.append(" ".join(toks))
            text = "\\N".join(parts)
            events.append(f"Dialogue: 0,{_ts(t0)},{_ts(t1)},Sub,,0,0,0,,{{\\pos({x},{placement.y})}}{text}")
    if hook_text:
        events.insert(0, f"Dialogue: 1,{_ts(0)},{_ts(hook_sec)},Hook,,0,0,0,,"
                         f"{{\\pos({x},{placement.hook_y})\\fad(0,250)}}"
                         + "\\N".join(_escape(l) for l in textwrap.wrap(hook_text, 24)[:3]))
    return header + "\n".join(events) + "\n"


def _escape(t: str) -> str:
    return t.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"
