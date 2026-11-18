"""Entry point: python -m autoclipper"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

import uvicorn

from .config import Config


def setup_logging(data_dir: Path) -> None:
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(data_dir / "logs" / "autoclipper.log",
                                              maxBytes=20 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)


def main() -> None:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    setup_logging(data_dir)
    from .web.app import create_app
    cfg = Config(data_dir)
    app = create_app(str(data_dir))
    uvicorn.run(app, host=cfg.get("dashboard.host"), port=int(os.environ.get("PORT", cfg.get("dashboard.port"))),
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
