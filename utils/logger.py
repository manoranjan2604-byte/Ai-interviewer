"""
utils/logger.py
Centralized logging configuration. Writes to both console and rotating
files under the logs/ directory, with separate loggers for major
subsystems so logs stay easy to search.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from config import config

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_configured_loggers = {}


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger. Safe to call repeatedly for the same name."""
    if name in _configured_loggers:
        return _configured_loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter(_LOG_FORMAT)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        log_file = os.path.join(config.LOGS_DIR, f"{name}.log")
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _configured_loggers[name] = logger
    return logger
