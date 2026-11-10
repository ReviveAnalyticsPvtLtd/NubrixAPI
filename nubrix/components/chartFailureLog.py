"""One structured warning per failed chart. No SQL, no user text, no data values."""

import json
import re

from utils.logger import logger

_MAX_ERROR_CHARACTERS = 300

# One left-to-right pass, so a quote inside another kind of text is never misread:
#   1. a double-quoted identifier is kept as it is;
#   2. a single-quoted literal ('' is an escaped quote) becomes '?', and so does one left open at the line's end (an apostrophe right after a
#      letter, as in "can't", is a contraction and opens nothing);
#   3. a standalone number (digits not preceded by a letter, digit or underscore, with joined parts such as
#      12.5, 2024-13-01, 10:30:00, 1e5) becomes ?. Type names like INT32 and FLOAT8 do not match.
_REDACT = re.compile(
    r'(?P<identifier>"(?:[^"]|"")*")'
    r"|(?P<literal>(?<![A-Za-z])(?:'(?:[^']|'')*'|'[^']*$))"
    r"|(?P<number>(?<![A-Za-z0-9_])\d+(?:[.:/-]\d+)*(?:[eE][+-]?\d+)?)"
)


def _replace(match: re.Match) -> str:
    if match.group("identifier") is not None:
        return match.group("identifier")
    if match.group("literal") is not None:
        return "'?'"
    return "?"


def _redactedFirstLine(error: BaseException | str) -> str:
    text = str(error)
    firstLine = text.split("\n", 1)[0]
    return _REDACT.sub(_replace, firstLine)[:_MAX_ERROR_CHARACTERS]


def logChartFailure(
    *,
    stage: str,
    error: BaseException | str,
    traceId: str | None,
    projectId: str,
    chartType: str | None,
    repairs: int,
    shadow: bool = False,
) -> None:
    """Log the stage, the error class, the redacted first line, and the ids needed to find the trace.

    `errorClass` is the exception's class name, or "str" when the error is a plain string.
    The cut to 300 characters comes after the redaction, so it never leaves half a literal.
    """
    record = {
        "stage": stage,
        "errorClass": "str" if isinstance(error, str) else type(error).__name__,
        "error": _redactedFirstLine(error),
        "traceId": traceId,
        "projectId": projectId,
        "chartType": chartType,
        "repairs": repairs,
        "shadow": shadow,
    }
    logger.warning("chart_failure " + json.dumps(record, sort_keys=True))


def traceIdOf(config: dict | None) -> str | None:
    """The Langfuse trace id of the first callback in a LangChain config that has one."""
    for callback in (config or {}).get("callbacks") or []:
        traceId = getattr(callback, "last_trace_id", None)
        if traceId:
            return traceId
    return None
