"""
sqlCheck.py

Checks a generated chart query before it is sent to the sandbox. The check
runs in the API process and never executes the SQL: it parses the text with
sqlglot (DuckDB dialect) and accepts exactly one read-only SELECT that reads
only this project's tables, CTEs defined in the same query, subqueries and
VALUES lists.

The rule is default-deny. The statement type, every FROM/JOIN source and every
table reference must be on the allowlist, and the whole tree is walked, so a
forbidden construct is refused in a subquery, CTE body, set operation, lateral
join or scalar position exactly as it is at the top. Messages name the
construct and say what is allowed instead, because they go to the repair prompt.

This is one of four layers (this allowlist, DuckDB external access off with a
locked configuration, the OS sandbox, and only this run's tables spooled).
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["CheckedSql", "SqlCheckError", "checkSql"]

import string
from dataclasses import dataclass
from typing import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

_DIALECT = "duckdb"
_MAX_LISTED_TABLES = 30

# Refused anywhere in the query, scalar or table position, case-insensitive.
_FORBIDDEN_PREFIXES = ("read_", "duckdb_", "pragma_", "parquet_", "iceberg_", "delta_")
_FORBIDDEN_NAMES = frozenset(
    {
        "sniff_csv",
        "glob",
        "query",
        "query_table",
        "current_setting",
        "getvariable",
        "getenv",
        "which_secret",
        "json_execute_serialized_sql",
        "load_extension",
    }
)

_DENIED_NODE_NAMES = (
    "Command",
    "Attach",
    "Detach",
    "Copy",
    "Pragma",
    "Set",
    "Install",
    "Create",
    "Drop",
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Describe",
    "Summarize",
    "Show",
    "Use",
    "Alter",
    "AlterSet",
    "Export",
    "Into",
    "Transaction",
    "Commit",
    "Rollback",
    "Analyze",
    "TruncateTable",
    "Comment",
    "Refresh",
    "LoadData",
    "Execute",
    "Kill",
    "Cache",
    "Uncache",
    "Declare",
    "Grant",
    "Revoke",
    "Placeholder",
    "Parameter",
    "SessionParameter",
)
_DENIED_NODES = tuple(getattr(exp, name) for name in _DENIED_NODE_NAMES if hasattr(exp, name))

_ALLOWED_ROOT = "Write one SELECT statement (optionally with WITH, UNION/INTERSECT/EXCEPT, subqueries and window functions)."
_ALLOWED_SOURCES = "Select from the project tables, a CTE defined with WITH in this query, a subquery or a VALUES list."


class SqlCheckError(ValueError):
    """The query is not a single read-only SELECT over this project's tables."""


@dataclass(frozen=True)
class CheckedSql:
    """A query that passed the check.

    `tables` holds the canonical project table names it references, each once,
    in first-seen order, without CTE names.
    """

    sql: str
    tables: tuple[str, ...]


_ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _foldName(name: str) -> str:
    """Fold a name exactly as DuckDB does: ASCII letters only.

    str.casefold() would also fold characters such as U+017F or U+212A onto
    ASCII letters, so a CTE the check sees as a different spelling could
    resolve to a system view or file in DuckDB.
    """
    return str(name).translate(_ASCII_FOLD)


def _isForbiddenFunction(name: str) -> bool:
    lowered = _foldName(name)
    return lowered in _FORBIDDEN_NAMES or lowered.startswith(_FORBIDDEN_PREFIXES)


def _functionName(node: exp.Expression) -> str:
    """The function name as the user most likely wrote it."""
    if isinstance(node, exp.Anonymous):
        return str(node.name)
    if isinstance(node, exp.GenerateSeries):
        return "range/generate_series"
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return type(node).__name__.lower()


def _statementLabel(node: exp.Expression) -> str:
    if isinstance(node, exp.Command):
        word = str(node.this or "").split(None, 1)
        return word[0].upper() if word else "COMMAND"
    return type(node).__name__.upper()


def _firstLine(error: Exception) -> str:
    lines = str(error).strip().splitlines()
    return (lines[0].strip() if lines else type(error).__name__).rstrip(".")


class _Checker:
    def __init__(self, text: str, activeByKey: dict[str, str]) -> None:
        self.text = text
        self.activeByKey = activeByKey
        self.found: dict[str, int] = {}
        self.counter = 0

    # -- tree walk ---------------------------------------------------------

    def visit(self, node: exp.Expression, ctes: frozenset[str]) -> None:
        self._checkNode(node, ctes)
        withNode = node.args.get("with_") or node.args.get("with")
        if not isinstance(withNode, exp.With):
            for child in node.iter_expressions():
                self.visit(child, ctes)
            return

        # CTE names are visible to the CTEs after them and to the query body
        # that owns the WITH. A recursive CTE also sees itself; a plain one
        # sees the project table of the same name inside its own body.
        recursive = bool(withNode.args.get("recursive"))
        scope = ctes
        for cte in withNode.expressions:
            name = _foldName(cte.alias_or_name)
            inner = scope | {name} if recursive else scope
            self._checkNode(cte, inner)
            for child in cte.iter_expressions():
                self.visit(child, inner)
            scope = scope | {name}
        for child in node.iter_expressions():
            if child is not withNode:
                self.visit(child, scope)

    def _checkNode(self, node: exp.Expression, ctes: frozenset[str]) -> None:
        if isinstance(node, _DENIED_NODES):
            label = _statementLabel(node)
            if label == "INTO":
                raise SqlCheckError("SELECT ... INTO is not allowed because it writes a table. " + _ALLOWED_ROOT)
            if label in ("PLACEHOLDER", "PARAMETER", "SESSIONPARAMETER"):
                raise SqlCheckError(
                    "Query parameters (? or $1) are not allowed because no values are supplied. "
                    "Write the value into the query as a literal."
                )
            raise SqlCheckError(f"{label} is not allowed inside a query. {_ALLOWED_ROOT}")

        if isinstance(node, exp.Func) and _isForbiddenFunction(_functionName(node)):
            self._rejectFunction(_functionName(node))

        if isinstance(node, exp.Lateral) and not isinstance(node.this, exp.Subquery):
            raise SqlCheckError(
                f"LATERAL {_functionName(node.this)}() is a table function and is not allowed. "
                "Use LATERAL with a (SELECT ...) subquery, or use a scalar function in the SELECT list instead."
            )

        if isinstance(node, (exp.From, exp.Join)):
            source = node.this
            if source is not None and not isinstance(
                source, (exp.Table, exp.Subquery, exp.Values, exp.Lateral, exp.Select, exp.SetOperation)
            ):
                self._rejectTableFunction(_functionName(source))

        if isinstance(node, exp.Table):
            self._checkTable(node, ctes)

    def _checkTable(self, table: exp.Table, ctes: frozenset[str]) -> None:
        ident = table.this
        if not isinstance(ident, exp.Identifier):
            self._rejectTableFunction(_functionName(ident) if ident is not None else "?")

        name = str(ident.name)
        db, catalog = table.args.get("db"), table.args.get("catalog")
        if db or catalog:
            qualified = ".".join(str(part.name) for part in (catalog, db, ident) if part)
            raise SqlCheckError(
                f"Table name '{qualified}' is schema- or catalog-qualified, which is not allowed. "
                f"Use the bare table name, for example FROM {name}."
            )

        start = ident.meta.get("start")
        if ident.args.get("quoted"):
            # FROM 'file.csv' reads a file in DuckDB; sqlglot parses it as a
            # quoted name, so look at the character in the original text.
            if start is None or self.text[start : start + 1] == "'":
                raise SqlCheckError(
                    f"A string literal used as a source ({self.text[start:start + 40] if start is not None else name}) "
                    "can read a file and is not allowed. " + _ALLOWED_SOURCES
                )

        key = _foldName(name)
        if key in ctes:
            return
        canonical = self.activeByKey.get(key)
        if canonical is None:
            names = sorted(set(self.activeByKey.values()))
            shown = ", ".join(f'"{n}"' for n in names[:_MAX_LISTED_TABLES])
            if len(names) > _MAX_LISTED_TABLES:
                shown += ", ..."
            raise SqlCheckError(
                f"Table '{name}' is not available. "
                f"Use one of the project tables ({shown or 'none are active'}) or a CTE defined with WITH in this query."
            )
        self.counter += 1
        position = start if start is not None else len(self.text) + self.counter
        if canonical not in self.found or position < self.found[canonical]:
            self.found[canonical] = position

    # -- messages ----------------------------------------------------------

    @staticmethod
    def _rejectFunction(name: str) -> None:
        raise SqlCheckError(
            f"Function {name}() is not allowed because it reads files, settings or the environment. "
            "Use ordinary SQL functions over the project tables only."
        )

    @staticmethod
    def _rejectTableFunction(name: str) -> None:
        raise SqlCheckError(
            f"Table function {name}() in FROM or JOIN is not allowed. " + _ALLOWED_SOURCES
        )

    # -- token level -------------------------------------------------------

    def checkFunctionTokens(self) -> None:
        """Refuse forbidden names in the original spelling.

        sqlglot maps some DuckDB functions to its own classes (`range` becomes
        GENERATE_SERIES), so the tree can hide the name the user wrote. A name
        directly before an opening parenthesis is a function call.
        """
        try:
            tokens = sqlglot.Dialect.get_or_raise(_DIALECT).tokenize(self.text)
        except SqlglotError as error:
            raise SqlCheckError(f"The SQL could not be read: {_firstLine(error)}. {_ALLOWED_ROOT}") from error
        for current, following in zip(tokens, tokens[1:]):
            if following.token_type.name == "L_PAREN" and current.token_type.name != "STRING":
                if _isForbiddenFunction(current.text):
                    self._rejectFunction(current.text)


def checkSql(sql: str, activeTables: Iterable[str]) -> CheckedSql:
    """Check `sql` against the read-only allowlist.

    Table names match `activeTables` case-insensitively and come back in their
    canonical spelling. Raises SqlCheckError with a message for the repair
    prompt when the query is refused. Never executes anything.
    """
    activeByKey: dict[str, str] = {}
    for name in activeTables:
        activeByKey.setdefault(_foldName(name), str(name))

    text = sql.strip() if isinstance(sql, str) else ""
    while text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        raise SqlCheckError(f"The query is empty. {_ALLOWED_ROOT}")

    try:
        statements = [s for s in sqlglot.parse(text, read=_DIALECT) if s is not None]
    except SqlglotError as error:
        raise SqlCheckError(f"The SQL could not be parsed: {_firstLine(error)}. {_ALLOWED_ROOT}") from error
    except RecursionError as error:
        raise SqlCheckError("The query is nested too deeply. Write a simpler query.") from error

    if not statements:
        raise SqlCheckError(f"The query is empty. {_ALLOWED_ROOT}")
    if len(statements) != 1:
        raise SqlCheckError(f"Only one statement is allowed, found {len(statements)}. {_ALLOWED_ROOT}")

    root = statements[0]
    body = root
    while isinstance(body, exp.Subquery) and not body.args.get("alias") and not body.args.get("pivots"):
        body = body.this
    if not isinstance(body, (exp.Select, exp.SetOperation)):
        label = _statementLabel(root) if isinstance(root, _DENIED_NODES) else text.split(None, 1)[0].upper()
        raise SqlCheckError(f"{label} is not allowed: only a single read-only SELECT is allowed. {_ALLOWED_ROOT}")

    checker = _Checker(text, activeByKey)
    try:
        checker.visit(root, frozenset())
        checker.checkFunctionTokens()
    except RecursionError as error:
        raise SqlCheckError("The query is nested too deeply. Write a simpler query.") from error

    tables = tuple(sorted(checker.found, key=checker.found.__getitem__))
    return CheckedSql(sql=text, tables=tables)
