"""Publishing worker: `python -m autoclipper.worker`

Runs as its own container next to the recorder/dashboard. It uploads due jobs, verifies
them, refreshes tokens, collects analytics and sends notifications. It writes a heartbeat
the dashboard and Docker healthcheck read. All state is in SQLite, so it can be killed and
restarted at any moment.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import analytics
from .__main__ import setup_logging
from .config import Config
from .db import DB
from .notify import Notifier
from .publishing import Engine
from .secure import Vault

log = logging.getLogger("autoclipper.worker")
HEARTBEAT_KEY = "worker_heartbeat"


def build(data_dir: Path) -> tuple[Config, DB, Engine, Notifier]:
    cfg = Config(data_dir)
    db = DB(cfg.db_path)
    vault = Vault(data_dir, db)
    notifier = Notifier(cfg, db, vault)
    return cfg, db, Engine(cfg, db, vault, notifier), notifier


def main() -> None:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    setup_logging(data_dir, "worker.log")
    cfg, db, engine, notifier = build(data_dir)
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    engine.recover()
    last = {"plan": 0.0, "verify": 0.0, "metrics": 0.0}
    log.info("publishing worker started (dry_run=%s, emergency_stop=%s)",
             cfg.get("publishing.dry_run"), cfg.get("publishing.emergency_stop"))
    while not stop["flag"]:
        cfg.reload()
        now = time.time()
        db.kv_set(HEARTBEAT_KEY, {"t": now, "pid": os.getpid()})
        try:
            if now - last["plan"] > 120:
                engine.plan_recent()
                last["plan"] = now
            engine.tick()
            if now - last["verify"] > 1800:
                for acc in db.query("SELECT * FROM accounts WHERE status IN ('connected','expired')"):
                    engine.verify_account(acc)
                    exp = acc.get("refresh_expires_at")
                    if exp and 0 < exp - now < 7 * 86400:
                        notifier.send("account_problem", f"⏳ {acc['provider']} authorization for "
                                      f"{acc.get('display_name')} expires in {int((exp - now) / 86400)} days; reconnect it.")
                last["verify"] = now
            if cfg.get("analytics.enabled") and now - last["metrics"] > float(cfg.get("analytics.interval_hours")) * 3600:
                n = analytics.collect(engine)
                log.info("analytics: stored %d metric snapshots", n)
                last["metrics"] = now
            if cfg.get("notifications.events.daily_summary") and notifier.daily_summary_due():
                notifier.send("daily_summary", notifier.daily_summary())
                db.kv_set("daily_summary_sent", time.strftime("%Y-%m-%d"))
        except Exception:  # noqa: BLE001 - the loop must never die
            log.exception("worker loop error")
        for _ in range(15):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("worker stopped")


def healthcheck() -> int:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    db = DB(Config(data_dir).db_path)
    hb = db.kv_get(HEARTBEAT_KEY) or {}
    return 0 if time.time() - hb.get("t", 0) < 180 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "health":
        sys.exit(healthcheck())
    main()
