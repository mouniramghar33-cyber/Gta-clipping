"""Performance tracking and learning.

Metrics are only stored when a platform API returns them; a missing metric stays NULL and
is never estimated. Learning compares each post with its own account's median views
(relative performance), so a big and a small account can be combined:

  * streamer_bonus  -> added to a clip's score before the publish threshold (max +/-10)
  * pick_style      -> best-performing editing style per account (with some exploration)
  * best_times      -> best posting hours per account
Nothing kicks in until `analytics.min_samples` posts exist for a group.
"""
from __future__ import annotations

import hashlib
import logging
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def collect(engine) -> int:
    """Fetch metrics for recent published jobs from every provider that supports it."""
    cfg, db = engine.cfg, engine.db
    since = time.time() - float(cfg.get("analytics.max_age_days")) * 86400
    jobs = db.query("SELECT * FROM publish_jobs WHERE status='published' AND published_at>?", (since,))
    by_acc = defaultdict(list)
    for j in jobs:
        by_acc[j["account_id"]].append(j)
    stored = 0
    for acc_id, js in by_acc.items():
        acc = engine.account(acc_id)
        if not acc or acc["status"] != "connected":
            continue
        try:
            res = engine.providers[acc["provider"]].metrics(acc, js)
        except Exception as e:  # noqa: BLE001
            log.warning("metrics for account %s failed: %s", acc_id, e)
            continue
        jmap = {j["id"]: j for j in js}
        for jid, m in res.items():
            if not any(v is not None for v in m.values()):
                continue
            j = jmap[jid]
            db.insert("metrics", {
                "job_id": jid, "fetched_at": time.time(),
                "age_hours": (time.time() - (j["published_at"] or j["created_at"])) / 3600,
                **{k: m.get(k) for k in ("views", "likes", "comments", "shares", "watch_time_min",
                                         "avg_view_pct", "subs_gained")},
                "raw": m})
            stored += 1
    return stored


def _latest(db) -> list[dict]:
    """Latest metric snapshot per published job, joined with clip/account info."""
    return db.query("""
        SELECT j.id job_id, j.account_id, j.style, j.published_at, j.url, j.title,
               c.streamer, c.top_cat, c.layout, c.score, c.duration,
               a.provider, a.platform, a.display_name,
               m.views, m.likes, m.comments, m.shares, m.watch_time_min, m.avg_view_pct, m.subs_gained,
               m.age_hours, m.fetched_at
        FROM publish_jobs j JOIN clips c ON c.id=j.clip_id JOIN accounts a ON a.id=j.account_id
        JOIN metrics m ON m.id = (SELECT id FROM metrics WHERE job_id=j.id ORDER BY fetched_at DESC LIMIT 1)
        WHERE j.status='published'""")


def relative(rows: list[dict], min_age_h: float = 24) -> list[dict]:
    """Attach rel = views / account median views (only posts old enough to compare)."""
    by_acc = defaultdict(list)
    for r in rows:
        if r["views"] is not None and (r["age_hours"] or 0) >= min_age_h:
            by_acc[r["account_id"]].append(r["views"])
    med = {a: statistics.median(v) for a, v in by_acc.items() if v}
    out = []
    for r in rows:
        m = med.get(r["account_id"])
        if r["views"] is not None and m and (r["age_hours"] or 0) >= min_age_h:
            out.append({**r, "rel": r["views"] / m if m > 0 else 1.0})
    return out


def _group(rows, key, min_n):
    g = defaultdict(list)
    for r in rows:
        g[key(r)].append(r["rel"])
    return {k: {"n": len(v), "rel": round(statistics.mean(v), 3)} for k, v in g.items() if len(v) >= min_n}


def insights(db, cfg) -> dict:
    rows = _latest(db)
    rel = relative(rows)
    n = int(cfg.get("analytics.min_samples"))
    tz = ZoneInfo(cfg.get("publishing.timezone"))
    hour = lambda r: datetime.fromtimestamp(r["published_at"], tz).hour  # noqa: E731
    totals = {"posts_with_metrics": len(rows), "comparable_posts": len(rel)}
    for k in ("views", "likes", "comments", "shares", "watch_time_min", "subs_gained"):
        vals = [r[k] for r in rows if r[k] is not None]
        totals[k] = sum(vals) if vals else None   # None = no platform reported it
    return {
        "totals": totals,
        "by_streamer": _group(rel, lambda r: r["streamer"], n),
        "by_style": _group(rel, lambda r: r["style"] or "clean", n),
        "by_reaction": _group(rel, lambda r: r["top_cat"] or "unknown", n),
        "by_hour": _group(rel, hour, n),
        "by_layout": _group(rel, lambda r: r["layout"], n),
        "by_platform": _group(rel, lambda r: r["platform"], n),
        "posts": sorted(rows, key=lambda r: -(r["views"] or 0))[:100],
    }


def streamer_bonus(db, cfg, streamer: str) -> float:
    rel = [r for r in relative(_latest(db)) if r["streamer"] == streamer]
    if len(rel) < int(cfg.get("analytics.min_samples")):
        return 0.0
    m = statistics.mean(r["rel"] for r in rel)
    return round(max(-10.0, min(10.0, 10 * math.log2(max(m, 1e-3)))), 1)


def pick_style(db, cfg, account: dict, clip_id: int) -> str:
    styles = list((cfg.get("styles") or {}).keys())
    rel = [r for r in relative(_latest(db)) if r["account_id"] == account["id"]]
    g = _group(rel, lambda r: r["style"] or "clean", int(cfg.get("analytics.min_samples")))
    # deterministic exploration: same clip always gets the same choice
    h = int(hashlib.sha256(f"{account['id']}:{clip_id}".encode()).hexdigest(), 16)
    untested = [s for s in styles if s not in g]
    if untested and (not g or (h % 1000) / 1000 < float(cfg.get("analytics.explore_rate"))):
        return untested[h % len(untested)]
    if g:
        return max(g, key=lambda s: g[s]["rel"])
    return styles[h % len(styles)]


def best_times(db, cfg, account: dict) -> list[str] | None:
    tz = ZoneInfo(cfg.get("publishing.timezone"))
    rel = [r for r in relative(_latest(db)) if r["account_id"] == account["id"]]
    g = _group(rel, lambda r: datetime.fromtimestamp(r["published_at"], tz).hour, int(cfg.get("analytics.min_samples")))
    if len(g) < 2:
        return None
    best = sorted(g, key=lambda h: -g[h]["rel"])[:4]
    return [f"{h:02d}:15" for h in sorted(best)]
