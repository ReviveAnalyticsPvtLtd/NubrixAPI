"""The CHART_ENGINE switch: ``off`` (default, also the rollback), ``shadow`` or ``on``."""

import os
from typing import Literal

from utils.logger import logger

ChartEngineMode = Literal["off", "shadow", "on"]

_MODES = ("off", "shadow", "on")
_warnedInvalid = False


def chartEngineMode() -> ChartEngineMode:
    """Read CHART_ENGINE on each call. Missing means off; any other unknown value means off, with one warning per process."""
    global _warnedInvalid
    value = os.environ.get("CHART_ENGINE")
    if value is None or value == "":
        return "off"
    if value in _MODES:
        return value  # type: ignore[return-value]
    if not _warnedInvalid:
        _warnedInvalid = True
        logger.warning("CHART_ENGINE has an unknown value; using off")
    return "off"
