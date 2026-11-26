"""
logger.py

This module configures the Loguru logger for the application.
It sets up:
- Colored console logging for better readability during development.
- File logging with log rotation for persistent storage and debugging.

The logger writes:
- To stdout with color formatting for human-friendly logs.
- To 'logs/runLogs.log' with DEBUG level and 1 MB file rotation.

Usage:
    from logger import logger
    logger.info("Something happened")
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["logger"]


from logtail import LogtailHandler
from loguru import logger
import sys
import os

# Other users' details land here, and sandbox runs have their own uids.
os.makedirs("logs", mode=0o700, exist_ok=True)

logger.remove()

_logtail_token = os.environ.get("LOGTAIL_TOKEN")
if _logtail_token:
    _logtail_host = os.environ.get("LOGTAIL_HOST")
    logger.add(
        LogtailHandler(
            source_token=_logtail_token,
            **({"host": _logtail_host} if _logtail_host else {}),
        ),
        level="INFO",
    )
logger.add(
    sys.stdout,
    colorize=True,
    format="<green>{time}</green> | <level>{level}</level> | <cyan>{message}</cyan>",
    level="INFO",
)
logger.add(
    "logs/runLogs.log",
    level="DEBUG",
    rotation="1 MB",
    diagnose=False,
)
