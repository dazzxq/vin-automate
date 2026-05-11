"""Shared logging setup for all pipeline CLIs.

Writes to BOTH stderr (visible in Cowork tool output) AND
`logs/pipeline.log` (RotatingFileHandler, 5MB × 3 backups).

The persistent log file is what BOOTSTRAP.md Step 5 / setup.skill Step 5
use to detect a scheduled-task run actually started (Phase A startup
detection). Without it, fast successful runs cannot be observed.
"""

from __future__ import annotations

import logging
import logging.handlers

import config


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_FILE_MAX_BYTES = 5 * 1024 * 1024
_FILE_BACKUPS = 3
_LOGFILE = config.LOGS_DIR / "pipeline.log"


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured with both stderr and rotating-file handlers.

    Idempotent: re-calling with the same name does NOT add duplicate handlers.
    """
    log = logging.getLogger(name)
    if getattr(log, "_vin_configured", False):
        return log

    log.setLevel(logging.INFO)
    log.propagate = False

    fmt = logging.Formatter(_LOG_FORMAT)

    # stderr handler — visible to Cowork
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)

    # File handler — persistent, used by bootstrap startup detection
    try:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            _LOGFILE,
            maxBytes=_FILE_MAX_BYTES,
            backupCount=_FILE_BACKUPS,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:
        # File handler is best-effort; we still have stderr.
        log.warning("Could not open log file %s: %s", _LOGFILE, e)

    log._vin_configured = True  # type: ignore[attr-defined]
    return log
