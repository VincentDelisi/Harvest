"""Structured logging with rich console + rotating file. See STRATEGY_SPEC.md §12."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler

from engine.utils.config import CONFIG

_LOG_DIR = Path("./logs")
_LOG_DIR.mkdir(exist_ok=True)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(CONFIG.log_level)

    console = RichHandler(rich_tracebacks=True, show_time=True, show_path=False)
    console.setLevel(CONFIG.log_level)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        _LOG_DIR / "engine.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=30,
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger
