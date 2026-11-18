"""End-to-end test against a REAL live Twitch stream.

    python tests/e2e_live.py <channel|watchlist> [max_seconds] [--sensitive]

Runs the full system (live check, recording, chat, detection, Whisper, render, scoring)
in a temporary data directory and prints the clips it produced. --sensitive lowers the
detection thresholds so a short test run is likely to produce a clip.
"""
import asyncio
import os
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autoclipper.config import Config  # noqa: E402
from autoclipper.db import DB  # noqa: E402
from autoclipper.manager import Manager  # noqa: E402


async def main(channel: str, secs: int, sensitive: bool, data: str) -> int:
    cfg = Config(data)
    patch = {"live_check_interval_sec": 30}
    if channel != "watchlist":       # "watchlist" = the verified 11-streamer watchlist only
        patch["streamers"] = [channel]
    if sensitive:
        patch.update({"chat": {"min_baseline_windows": 6},
                      "detection": {"z_threshold": 1.6, "ratio_threshold": 1.4, "min_messages": 5}})
    cfg.update(patch)
    db = DB(cfg.db_path)
    mgr = Manager(cfg, db)
    await mgr.start()
    t0 = time.time()
    while time.time() - t0 < secs and db.one("SELECT COUNT(*) n FROM clips")["n"] < int(os.environ.get("E2E_CLIPS", 2)):
        await asyncio.sleep(15)
    while mgr.inflight:
        await asyncio.sleep(5)
    await mgr.stop()
    clips = db.query("SELECT id, streamer, platform, duration, score, tier, scores, title, hook, layout, status, gta, "
                     "collab, labels, latency, path FROM clips")
    for c in clips:
        c["scores"].pop("fingerprint", None)
        c["labels"] = {k: c["labels"].get(k) for k in ("primary", "labels", "gta_tags", "entertaining", "reason")} if c["labels"] else None
        print(json.dumps(c, ensure_ascii=False))
    for m in db.query("SELECT id, status, error, peak_ratio FROM moments"):
        print("moment", m)
    return 0 if clips else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ch = sys.argv[1]
    secs = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 900
    data = tempfile.mkdtemp(prefix="autoclipper-e2e-")
    print("data dir:", data)
    sys.exit(asyncio.run(main(ch, secs, "--sensitive" in sys.argv, data)))
