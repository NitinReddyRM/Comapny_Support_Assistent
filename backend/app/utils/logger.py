"""
Centralized logging utilities.

We maintain one logger per concern (auth, chatbot, errors, feedback,
security, audit, admin, api, performance, guardrail, ai). Each has its
own rotating file under LOG_DIR. Loggers are idempotent — calling
get_logger() multiple times returns the same instance.
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict

from app.config import settings

_LOGGERS: Dict[str, logging.Logger] = {}

LOG_FILES = {
    "auth": "auth.log",
    "chatbot": "chatbot.log",
    "errors": "errors.log",
    "feedback": "feedback.log",
    "security": "security.log",
    "audit": "audit.log",
    "admin": "admin.log",
    "api": "api.log",
    "performance": "performance.log",
    "guardrail": "guardrail.log",
    "ai": "ai.log",
}

_FMT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | "
    "pid=%(process)d | %(message)s"
)


def _ensure_log_dir() -> Path:
    p = Path(settings.LOG_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_logger(name: str) -> logging.Logger:
    """Return (and lazily build) a named rotating logger."""
    if name in _LOGGERS:
        return _LOGGERS[name]

    log_dir = _ensure_log_dir()
    filename = LOG_FILES.get(name, f"{name}.log")
    log_path = log_dir / filename

    logger = logging.getLogger(f"company-ai.{name}")
    logger.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    logger.propagate = False

    handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.LOG_ROTATION_MB * 1024 * 1024,
        backupCount=settings.LOG_RETENTION_DAYS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(handler)

    if settings.APP_DEBUG:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(_FMT))
        logger.addHandler(sh)

    _LOGGERS[name] = logger
    return logger


def log_event(channel: str, level: str, message: str, **kwargs):
    """Structured log helper: appends key=value pairs to the message."""
    extra = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
    full = f"{message} | {extra}" if extra else message
    logger = get_logger(channel)
    getattr(logger, level.lower(), logger.info)(full)
