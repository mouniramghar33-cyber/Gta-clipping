"""Vertical 9:16 layout planning: black-border removal, facecam detection, subject tracking.

Layouts:
  stack - facecam panel on top, gameplay below (streamer with a facecam overlay)
  track - single 9:16 crop that follows a large face (Just Chatting / IRL)
  fit   - full gameplay on a blurred background (no face found), keeps all gameplay visible
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field

import cv2
import numpy as np

log = logging.getLogger(__name__)

DET_W = 960  # analysis width
YUNET_PATH = os.environ.get("YUNET_MODEL", "/opt/models/face_detection_yunet_2023mar.onnx")


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def scaled(self, s: float) -> "Box":
        return Box(self.x * s, self.y * s, self.w * s, self.h * s)


@dataclass
class LayoutPlan:
    kind: str                       # stack | track | fit
    src_w: int
    src_h: int
    content: Box                    # area without black borders (source pixels)
    face: Box | None = None         # face in source pixels
    cam: Box | None = None          # facecam crop (stack)
    game: Box | None = None         # gameplay crop (stack/fit)
    track_w: int = 0                # crop width for track mode
    track_x: list[tuple[float, float]] = field(default_factory=list)  # (t, x) keyframes
    top_h: int = 0                  # output height of the facecam panel (stack)
    face_out: Box | None = None     # face position in the output frame (for subtitle placement)
    cam_rect: Box | None = None     # detected facecam overlay borders (source pixels)
    game_out: Box | None = None     # gameplay position in the output frame


def probe_size(path: str) -> tuple[int, int, float]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=30).stdout.strip().splitlines()[0]
    w, h, fr = out.split(",")[:3]
    num, _, den = fr.partition("/")
    return int(w), int(h), float(num) / float(den or 1)


def sample_frames(path: str, start: float, dur: float, fps: float = 1.0) -> list[tuple[float, np.ndarray]]:
    w, h, _ = probe_size(path)
    dh = int(round(h * DET_W / w / 2) * 2)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", path,
           "-t", f"{dur:.3f}", "-vf", f"fps={fps},scale={DET_W}:{dh}", "-f", "rawvideo",
           "-pix_fmt", "bgr24", "pipe:1"]
    raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    size = DET_W * dh * 3
    frames = []
    for i in range(len(raw) // size):
        frames.append((i / fps, np.frombuffer(raw[i * size:(i + 1) * size], np.uint8).reshape(dh, DET_W, 3)))
    return frames


class FaceDetector:
    def __init__(self):
        self.yunet = None
        if os.path.exists(YUNET_PATH) and hasattr(cv2, "FaceDetectorYN"):
            try:
                self.yunet = cv2.FaceDetectorYN.create(YUNET_PATH, "", (320, 320), 0.75, 0.3, 50)
            except cv2.error as e:
                log.warning("YuNet unavailable (%s), using Haar cascade", e)
        self.haar = None
        if hasattr(cv2, "CascadeClassifier"):
            self.haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if self.yunet is None and self.haar is None:
            log.warning("no face detector available; using the full-gameplay layout")

    def detect(self, img: np.ndarray) -> list[tuple[Box, float]]:
        h, w = img.shape[:2]
        if self.yunet is not None:
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(img)
            if faces is None:
                return []
            return [(Box(*f[:4]), float(f[-1])) for f in faces]
        if self.haar is None:
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = self.haar.detectMultiScale(gray, 1.1, 6, minSize=(28, 28))
        return [(Box(*map(float, f)), 0.8) for f in found]


def content_box(frames: list[np.ndarray]) -> Box:
    """Bounding box of non-black content over all sampled frames (removes letter/pillar boxes)."""
    h, w = frames[0].shape[:2]
    acc = np.max(np.stack([f.max(axis=2) for f in frames]), axis=0)
    rows = np.where(acc.max(axis=1) > 24)[0]
    cols = np.where(acc.max(axis=0) > 24)[0]
    if len(rows) == 0 or len(cols) == 0:
        return Box(0, 0, w, h)
    x0, x1, y0, y1 = cols[0], cols[-1] + 1, rows[0], rows[-1] + 1
    # ignore tiny borders (< 1.5%), they're usually compression noise
    if x0 < w * 0.015: x0 = 0
    if w - x1 < w * 0.015: x1 = w
    if y0 < h * 0.015: y0 = 0
    if h - y1 < h * 0.015: y1 = h
    return Box(x0, y0, x1 - x0, y1 - y0)


def _cluster_faces(dets: list[tuple[float, Box]], n_frames: int, w: int) -> tuple[Box, float] | None:
    """Most persistent face position across frames: (median box, share of frames it appears in)."""
    if not dets:
        return None
    best, best_n = None, 0
    for _, b in dets:
        members = [d for _, d in dets if abs(d.cx - b.cx) < w * 0.06 and abs(d.cy - b.cy) < w * 0.06]
        if len(members) > best_n:
            best_n, best = len(members), members
    med = Box(*(float(np.median([getattr(m, k) for m in best])) for k in ("x", "y", "w", "h")))
    times = {t for t, d in dets if abs(d.cx - med.cx) < w * 0.06 and abs(d.cy - med.cy) < w * 0.06}
    return med, len(times) / max(n_frames, 1)


def _find_overlay(frames, fb: Box) -> Box | None:
    """Find the borders of a facecam overlay: straight edges that stay in place while the
    gameplay around them changes. Works at analysis resolution; returns None if unclear."""
    if len(frames) < 3:
        return None
    g = np.stack([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.int16) for _, f in frames])
    h, w = g.shape[1:]
    ex = np.median(np.abs(np.diff(g, axis=2)), axis=0)  # vertical edges  (h, w-1)
    ey = np.median(np.abs(np.diff(g, axis=1)), axis=0)  # horizontal edges (h-1, w)
    T = 18

    def best(profile_fn, lo, hi):
        lo, hi = int(max(lo, 0)), int(hi)
        if hi - lo < 2:
            return None
        vals = [profile_fn(i) for i in range(lo, hi)]
        i = int(np.argmax(vals))
        return lo + i if vals[i] >= 0.75 else None

    r0, r1 = int(max(fb.y - fb.h * 0.3, 0)), int(min(fb.y + fb.h * 1.3, h - 1))
    colc = lambda x: float(np.mean(ex[r0:r1, x] > T))  # noqa: E731
    left = best(colc, fb.x - fb.w * 5, fb.x - fb.w * 0.4)
    right = best(colc, fb.x + fb.w * 1.4, min(fb.x + fb.w * 6, w - 1))
    x0 = (left + 1) if left is not None else 0
    x1 = (right + 1) if right is not None else w
    rowc = lambda y: float(np.mean(ey[y, x0:x1] > T))  # noqa: E731
    top = best(rowc, fb.y - fb.h * 4, fb.y - fb.h * 0.3)
    bottom = best(rowc, fb.y + fb.h * 1.3, min(fb.y + fb.h * 6, h - 1))
    y0 = (top + 1) if top is not None else 0
    y1 = (bottom + 1) if bottom is not None else h
    if (left is None and right is None) or (x1 - x0) >= w * 0.9:
        return None
    return Box(float(x0), float(y0), float(x1 - x0), float(y1 - y0))


def _clamp_box(b: Box, bound: Box) -> Box:
    w, h = min(b.w, bound.w), min(b.h, bound.h)
    x = min(max(b.x, bound.x), bound.x + bound.w - w)
    y = min(max(b.y, bound.y), bound.y + bound.h - h)
    return Box(x, y, w, h)


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def plan_layout(path: str, start: float, dur: float, out_w: int = 1080, out_h: int = 1920) -> LayoutPlan:
    src_w, src_h, _ = probe_size(path)
    frames = sample_frames(path, start, dur, fps=1.0)
    if not frames:
        raise RuntimeError("no frames decoded for layout analysis")
    s = src_w / DET_W
    content = content_box([f for _, f in frames]).scaled(s)
    det = FaceDetector()
    dets: list[tuple[float, Box]] = []
    for t, f in frames:
        for b, score in det.detect(f):
            if score >= 0.6 and b.w >= 18:
                dets.append((t, b.scaled(s)))
    cl = _cluster_faces(dets, len(frames), src_w)
    plan = LayoutPlan("fit", src_w, src_h, content)

    if cl and cl[1] >= 0.4:
        face, share = cl
        plan.face = face
        rel_w = face.w / content.w
        cx_rel = (face.cx - content.x) / content.w
        in_side = cx_rel < 0.38 or cx_rel > 0.62 or (face.cy - content.y) / content.h > 0.6 \
            or (face.cy - content.y) / content.h < 0.3
        if rel_w >= 0.10 and not in_side or rel_w >= 0.16:
            _plan_track(plan, frames, det, s, out_w, out_h, start)
        else:
            plan.cam_rect = _find_overlay(frames, face.scaled(1 / s))
            if plan.cam_rect is not None:
                plan.cam_rect = plan.cam_rect.scaled(s)
            _plan_stack(plan, out_w, out_h)
    else:
        _plan_fit(plan, out_w, out_h)
    log.info("layout=%s src=%dx%d content=%s face=%s", plan.kind, src_w, src_h,
             (int(content.x), int(content.y), int(content.w), int(content.h)),
             None if not plan.face else (int(plan.face.x), int(plan.face.y), int(plan.face.w)))
    return plan


def _plan_stack(plan: LayoutPlan, out_w: int, out_h: int) -> None:
    plan.kind = "stack"
    top_h = _even(out_h * 0.38)
    plan.top_h = top_h
    face, c = plan.face, plan.content
    aspect = out_w / top_h
    cam_h = min(face.h * 3.2, c.h)
    cam_w = cam_h * aspect
    if cam_w > c.w:
        cam_w, cam_h = c.w, c.w / aspect
    bound = c
    r = plan.cam_rect
    if r is not None and r.w >= face.w * 1.5 and r.h >= face.h * 1.5:
        # crop inside the real facecam overlay so no gameplay or chat leaks into the panel
        bound = r
        if r.w / r.h > aspect:
            cam_h, cam_w = r.h, r.h * aspect
        else:
            cam_w, cam_h = r.w, r.w / aspect
    cam = _clamp_box(Box(face.cx - cam_w / 2, face.cy - cam_h * 0.45, cam_w, cam_h), bound)
    plan.cam = Box(_even(cam.x), _even(cam.y), _even(cam.w), _even(cam.h))
    bot_h = out_h - top_h
    g_aspect = out_w / bot_h
    g_h = c.h
    g_w = min(c.w, g_h * g_aspect)
    gx = c.x + (c.w - g_w) / 2
    plan.game = Box(_even(gx), _even(c.y), _even(g_w), _even(g_h))
    sc = top_h / plan.cam.h
    plan.face_out = Box((face.x - plan.cam.x) * sc, (face.y - plan.cam.y) * sc, face.w * sc, face.h * sc)
    plan.game_out = Box(0, top_h, out_w, bot_h)


def _plan_fit(plan: LayoutPlan, out_w: int, out_h: int) -> None:
    plan.kind = "fit"
    c = plan.content
    # Trim the far edges a little (to 3:2) so gameplay is bigger, but keep everything important.
    target_aspect = 1.5
    if c.w / c.h > target_aspect:
        g_w = c.h * target_aspect
        g = Box(c.x + (c.w - g_w) / 2, c.y, g_w, c.h)
    else:
        g = c
    plan.game = Box(_even(g.x), _even(g.y), _even(g.w), _even(g.h))
    gh = out_w * plan.game.h / plan.game.w
    plan.game_out = Box(0, (out_h - gh) / 2, out_w, gh)
    if plan.face:
        sc = out_w / plan.game.w
        f = plan.face
        plan.face_out = Box((f.x - plan.game.x) * sc, plan.game_out.y + (f.y - plan.game.y) * sc, f.w * sc, f.h * sc)


def _plan_track(plan: LayoutPlan, frames, det: FaceDetector, s: float, out_w: int, out_h: int,
                start: float) -> None:
    plan.kind = "track"
    c = plan.content
    crop_w = _even(min(c.w, c.h * out_w / out_h))
    plan.track_w = crop_w
    xs: list[tuple[float, float]] = []
    last = plan.face.cx
    for t, f in frames:
        found = [b.scaled(s) for b, sc in det.detect(f) if sc >= 0.6]
        if found:
            # follow the face closest to the previous position (stable subject)
            b = min(found, key=lambda b: abs(b.cx - last) - b.w * 0.5)
            last = b.cx
        xs.append((t, last))
    # smooth: moving average + dead zone so the camera doesn't jitter
    raw = np.array([x for _, x in xs])
    if len(raw) >= 3:
        raw = np.convolve(np.pad(raw, (1, 1), mode="edge"), np.ones(3) / 3, mode="valid")
    out: list[tuple[float, float]] = []
    cur = raw[0]
    for (t, _), x in zip(xs, raw):
        if abs(x - cur) > crop_w * 0.08:
            cur = x
        left = min(max(cur - crop_w / 2, c.x), c.x + c.w - crop_w)
        out.append((t, float(left)))
    plan.track_x = out
    sc = out_h / c.h
    f = plan.face
    left0 = out[0][1] if out else c.x
    plan.face_out = Box((f.x - left0) * sc, (f.y - c.y) * sc, f.w * sc, f.h * sc)
    plan.game_out = Box(0, 0, out_w, out_h)
