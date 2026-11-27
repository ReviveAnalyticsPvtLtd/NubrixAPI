"""Remove a currency from label text the chart model wrote when neither the question nor the data names one.

The chart model has added "USD" or "$" to data that names no currency. Only its own text is edited, before it runs and
is saved: a SQL request's title and labels, or a Python program's label literals. A chart is never edited, so values
and names from the data can't change, and a dashboard refresh shows the same labels.

Evidence is the question, table and column names, and sample values; descriptions are not, because the AI writes them.
Other units ("%", kg, hours) are often computed, so they are never removed.
"""

import ast
import json
import logging
import re

from utils.codeExecutor import _remove_code_fences

logger = logging.getLogger(__name__)

# One family per currency: a family named anywhere in the evidence allows all of its forms.
_FAMILIES = (
    ("$", "us$", "a$", "c$", "s$", "usd", "aud", "cad", "sgd", "dollar", "dollars"),
    ("r$", "brl", "real", "reais"),
    ("₹", "inr", "rs", "rupee", "rupees"),
    ("€", "eur", "euro", "euros"),
    ("£", "gbp"),
    ("¥", "jpy", "cny", "yen", "yuan"),
    ("aed", "dirham", "dirhams"),
)
_FAMILY = {form: index for index, forms in enumerate(_FAMILIES) for form in forms}
# Also ordinary words, names or abbreviations (CAD software, INR the blood test, Dollar General): only a unit is removed.
_AMBIGUOUS = {"cad", "inr", "aed", "cny", "dollar", "dollars", "euro", "euros", "rupee", "rupees", "reais", "yen", "yuan", "dirham", "dirhams"}

_EDGE_BEFORE, _EDGE_AFTER = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"
_TOKEN = (
    rf"{_EDGE_BEFORE}(?:US|[RACS])\$|[$₹€£¥]"
    rf"|{_EDGE_BEFORE}(?:USD|AUD|CAD|SGD|BRL|INR|EUR|GBP|JPY|CNY|AED){_EDGE_AFTER}"
    rf"|{_EDGE_BEFORE}(?:dollars?|rupees?|euros?|reais|yen|yuan|dirhams?){_EDGE_AFTER}"
    rf"|{_EDGE_BEFORE}(?-i:Rs){_EDGE_AFTER}\.?"  # case-sensitive: "RS" is a Brazilian state
)
_MENTION = re.compile(rf"(?:\b(?P<lead>in|of)\s+)?(?P<token>{_TOKEN})", re.IGNORECASE)
_SCALE = r"(?:hundreds|thousands|millions|billions|lakhs|crores)"
_AFTER_A_UNIT = re.compile(
    rf"\s*(?:$|[()\[\],/;:|-])|\s+(?:by|per|for|over|across|during|since|from|{_SCALE})\b", re.IGNORECASE
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_CURRENCY_COLUMN = re.compile(r"currenc|ccy", re.IGNORECASE)
# A number under such a name is a key, rate or amount ("CurrencyRateID"), unless it is an ISO code (840 for USD).
_NUMERIC_TYPE = re.compile(r"(?:u?int|bigint|smallint|long|float|double|halffloat|decimal|numeric|number)", re.IGNORECASE)
_CODE_COLUMN = re.compile(r"code|iso", re.IGNORECASE)
_UNWRAP = [
    (re.compile(r"\(\s*(?:in\s+)?[,/;]?\s*\)|\[\s*\]"), ""),  # empty brackets
    (re.compile(r"\(\s*[,/;]?\s*"), "("),
    (re.compile(r"\s*[,/;]\s*\)"), ")"),
    (re.compile(r"\s{2,}"), " "),
    (re.compile(r"^(?:[\s,/;:_]|-(?!\s*\.?\d))+"), ""),  # separators left at either end, never a minus sign
    (re.compile(r"(?:[\s,/;:_]|(?<!\d)-)+$"), ""),
]


def labelEvidence(question: object, metadata: object) -> str:
    """The text a currency may be taken from: the question, table and column names, and sample values."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = None
    parts = [question if isinstance(question, str) else ""]
    for table, entry in (metadata.items() if isinstance(metadata, dict) else ()):
        parts.append(str(table))
        if not isinstance(entry, dict):
            continue
        columns = entry.get("columns") if isinstance(entry.get("columns"), list) else []
        names = [str(column.get("name", "")) for column in columns if isinstance(column, dict)]
        parts += names
        if any(_holdsCurrencies(column) for column in columns if isinstance(column, dict)):
            parts += _FAMILY  # the data holds currencies, and one sample row can't list which
        sample = entry.get("sample_row")
        if isinstance(sample, dict):
            parts += [str(value) for value in sample.values()]
    return " ".join(parts).lower()


def _holdsCurrencies(column: dict) -> bool:
    name = str(column.get("name", ""))
    if not _CURRENCY_COLUMN.search(name):
        return False
    return bool(_CODE_COLUMN.search(name)) or not _NUMERIC_TYPE.match(str(column.get("type", "")))


def _evidenced(evidence: str) -> set[int]:
    words = set(re.split(r"[^a-z]+", evidence))
    families = {_FAMILY[word] for word in words if word in _FAMILY}
    families |= {_FAMILY[form] for form in _FAMILY if not form.isalpha() and form in evidence}
    return families


def _clean(text: str, evidenced: set[int]) -> tuple[str, set[str]]:
    """The text without each unsupported currency, and the tokens removed."""
    dropped = set()

    def drop(match: re.Match) -> str:
        token, lead = match.group("token"), (match.group("lead") or "").lower()
        word = token.lower().rstrip(".")
        if _FAMILY.get(word) in evidenced:
            return match.group(0)
        before = text[: match.start()].rstrip()
        leadGoes = lead == "in" or (lead == "of" and re.search(rf"\b{_SCALE}$", before, re.IGNORECASE))
        if word in _AMBIGUOUS:
            introduced = leadGoes or before.endswith(("(", "[", ",", "/", ";"))
            if not (introduced and _AFTER_A_UNIT.match(text, match.end())):
                return match.group(0)
        dropped.add(token)
        return "" if leadGoes or not lead else text[match.start(): match.start("token")]

    out = _MENTION.sub(drop, text)
    if not dropped:
        return text, dropped
    for pattern, replacement in _UNWRAP:
        out = pattern.sub(replacement, out)
    if not any(character.isalnum() for character in out):
        return text, set()  # a label made only of a currency says nothing once emptied
    if _NUMBER.findall(out) != _NUMBER.findall(text):
        return text, set()  # never at the cost of a number: "500$200" must not read 500200
    return out, dropped


def cleanLabels(labels: dict, evidence: str, ref: object = None, series: tuple = ()) -> dict:
    """The labels that change when an unsupported currency is removed, as {key: new text}. Never raises.

    `series` are the keys of legend names: if cleaning would give two of them the same name, none of them changes.
    """
    try:
        evidenced = _evidenced(evidence or "")
        changed, removed = {}, {}
        for key, text in labels.items():
            if isinstance(text, str):
                new, tokens = _clean(text, evidenced)
                if tokens:
                    changed[key], removed[key] = new, tokens
        if len({changed.get(key, labels[key]) for key in series}) < len({labels[key] for key in series}):
            for key in series:
                changed.pop(key, None)
        if changed:
            # Tokens only: labels can hold customer data.
            logger.warning(f"chart_label_currency ref={ref} removed={sorted(set().union(*(removed[key] for key in changed)))}")
        return changed
    except Exception as error:  # a label check must never cost the user their chart
        logger.warning(f"chart_label_currency ref={ref} check failed: {type(error).__name__}")
        return {}


def cleanCode(code: str, evidence: str, ref: object = None) -> str:
    """Generated Python with its chart label literals cleaned. Never raises.

    shortcut: only literals in a chart or dataset dict are checked, not f-strings or variables; widen it if made-up
    currencies still show up on the Python engine.
    """
    if not isinstance(code, str):
        return code
    try:
        program = _remove_code_fences(code)
        tree = ast.parse(program)
    except Exception:  # also MemoryError or RecursionError, from code nested too deep
        return code  # the debugger gets code that doesn't parse, and its answer is checked again
    try:
        literals, series = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            fields = {key.value: value for key, value in zip(node.keys, node.values) if isinstance(key, ast.Constant)}
            isChart, isDataset = "chartType" in fields, "data" in fields and "chartType" not in fields
            for key in ("title", "xLabels", "yLabels", "label") if isChart else ("label",) if isDataset else ():
                value = fields.get(key)
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    if isDataset:
                        series.append(len(literals))
                    literals.append(value)
        changed = cleanLabels({index: node.value for index, node in enumerate(literals)}, evidence, ref, tuple(series))
        if not changed:
            return code
        source = program.encode()  # ast columns count UTF-8 bytes
        starts = [0]
        for line in source.splitlines(keepends=True):
            starts.append(starts[-1] + len(line))
        for index in sorted(changed, key=lambda index: (literals[index].lineno, literals[index].col_offset), reverse=True):
            node = literals[index]
            start, end = starts[node.lineno - 1] + node.col_offset, starts[node.end_lineno - 1] + node.end_col_offset
            source = source[:start] + repr(changed[index]).encode() + source[end:]
        edited = source.decode()
        return code.replace(program, edited, 1) if program in code else edited
    except Exception as error:
        logger.warning(f"chart_label_currency ref={ref} check failed: {type(error).__name__}")
        return code


def labelTexts(chart: object) -> list[str]:
    """Every label and text card value a chart shows, for the eval's report: more than the check edits."""
    if not isinstance(chart, dict):
        return []
    texts = [chart.get(key) for key in ("title", "xLabels", "yLabels", "label")]
    if chart.get("chartType") == "card":
        texts.append(chart.get("data"))
    data = chart.get("data")
    if isinstance(data, dict) and isinstance(data.get("datasets"), list):
        texts += [dataset.get("label") for dataset in data["datasets"] if isinstance(dataset, dict)]
    return [text for text in texts if isinstance(text, str) and text]
