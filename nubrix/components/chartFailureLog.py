"""One structured warning per failed chart.

The record holds ids, the stage, the error class and the first line of the error with data values redacted
(quoted text, numbers and anything with a digit in it). The only text kept from the error is the project's
table names, DuckDB type names and ordinary words; the full error stays in the Langfuse trace.
"""

import json
import re
from collections.abc import Iterable

from utils.logger import logger

_MAX_ERROR_CHARACTERS = 300

# One left-to-right pass, so a quote inside another kind of text is never misread:
#   1. a double-quoted token is kept only when it is exactly one of the project's table names, else it is "?"
#      (DuckDB echoes values and SQL literals inside double quotes as well as identifiers);
#   2. a single-quoted literal becomes '?'. DuckDB prints the raw value, so a quote closes the literal only when
#      it is not followed by a word character ('D'Souza' is one literal; '' is an escaped quote). A literal that
#      never closes is redacted to the end of the line. An apostrophe right after a letter, as in "can't", is a
#      contraction and opens nothing;
#   3. any other word-like token (joined parts such as 12.5, 2024-13-01, 10:30:00, UUIDs, 0xFF, 5kg) that contains
#      a digit becomes ?, unless it is a DuckDB type name such as INT32 or FLOAT8.
_REDACT = re.compile(
    r'(?P<identifier>"(?:[^"]|"")*"|"[^"]*$)'
    r"|(?P<literal>(?<![A-Za-z])(?:'(?:''|'(?=\w)|[^'])*'(?!\w)|'.*$))"
    r"|(?P<token>\w+(?:[.:/+\-]\w+)*)"
)
_TYPE_NAME = re.compile(r"U?(?:TINY|SMALL|BIG|HUGE)?INT(?:1|2|4|8|16|32|64|128)?|FLOAT[48]")


def _redactor(tableNames: Iterable[str] | None):
    known = set(tableNames or ())

    def replace(match: re.Match) -> str:
        identifier = match.group("identifier")
        if identifier is not None:
            inner = identifier[1:-1] if len(identifier) > 1 and identifier.endswith('"') else None
            return identifier if inner in known else '"?"'
        if match.group("literal") is not None:
            return "'?'"
        token = match.group("token")
        if any(character.isdigit() for character in token) and not _TYPE_NAME.fullmatch(token):
            return "?"
        return token

    return replace


def _redactedFirstLine(error: BaseException | str, tableNames: Iterable[str] | None = None) -> str:
    firstLine = str(error).split("\n", 1)[0]
    return _REDACT.sub(_redactor(tableNames), firstLine)[:_MAX_ERROR_CHARACTERS]


def logChartFailure(
    *,
    stage: str,
    error: BaseException | str,
    traceId: str | None,
    projectId: str,
    chartType: str | None,
    repairs: int,
    shadow: bool = False,
    tableNames: Iterable[str] | None = None,
) -> None:
    """Log the stage, the error class, the redacted first line, and the ids needed to find the trace.

    `errorClass` is the exception's class name, or "str" when the error is a plain string.
    `tableNames` are the project's tables: the only double-quoted text kept from the error.
    The cut to 300 characters comes after the redaction, so it never leaves half a literal.
    """
    record = {
        "stage": stage,
        "errorClass": "str" if isinstance(error, str) else type(error).__name__,
        "error": _redactedFirstLine(error, tableNames),
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
