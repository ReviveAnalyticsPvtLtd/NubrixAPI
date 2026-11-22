"""Remove a currency from chart labels when neither the question nor the data names one.

The chart model writes titles and labels as free text and has added "USD" or "$" to data that names no currency.
Evidence is the user's question, table and column names, and sample values; descriptions are not evidence, because the
AI writes them too. Only label text changes, never numbers. Other units ("%", kg, hours) are often computed, so they are
never removed.
"""

import json
import logging
import re

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
_EDGE_BEFORE, _EDGE_AFTER = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"
_TOKEN = re.compile(
    r"(?:US|[RACS])?\$|[₹€£¥]"
    rf"|{_EDGE_BEFORE}(?:USD|AUD|CAD|SGD|BRL|INR|EUR|GBP|JPY|CNY|AED){_EDGE_AFTER}"
    rf"|{_EDGE_BEFORE}(?:dollars?|rupees?|euros?|reais|yen|yuan|dirhams?){_EDGE_AFTER}"
    rf"|{_EDGE_BEFORE}(?-i:Rs){_EDGE_AFTER}\.?",  # case-sensitive: "RS" is a Brazilian state
    re.IGNORECASE,
)
_UNWRAP = [
    (re.compile(r"\(\s*(?:in\s+)?[,/;]?\s*\)"), ""),  # an empty "()" or "(in )"
    (re.compile(r"\(\s*[,/;]?\s*"), "("),
    (re.compile(r"\s*[,/;]\s*\)"), ")"),
    (re.compile(r"\s{2,}"), " "),
]


def _family(token: str) -> int | None:
    return _FAMILY.get(token.lower().rstrip("."))


def labelEvidence(question: object, metadata: object) -> str:
    """The text a currency may be taken from: the question, table names, column names and sample values."""
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
        parts += [str(column.get("name", "")) for column in entry.get("columns") or [] if isinstance(column, dict)]
        sample = entry.get("sample_row")
        if isinstance(sample, dict):
            parts += [str(value) for value in sample.values()]
    return " ".join(parts).lower()


def _evidenced(evidence: str) -> set[int]:
    words = set(re.split(r"[^a-z]+", evidence))
    families = {_FAMILY[word] for word in words if word in _FAMILY}
    families |= {_FAMILY[form] for form in _FAMILY if not form.isalpha() and form in evidence}
    return families


def _clean(text: str, evidenced: set[int], removed: set[str]) -> str:
    dropped = set()

    def drop(match: re.Match) -> str:
        if _family(match.group("token")) in evidenced:
            return match.group(0)
        dropped.add(match.group("token"))
        return ""

    out = re.sub(rf"(?:\b[Ii]n\s+)?(?P<token>{_TOKEN.pattern})", drop, text, flags=re.IGNORECASE)
    if out == text:
        return text
    for pattern, replacement in _UNWRAP:
        out = pattern.sub(replacement, out)
    out = out.strip(" ,/;:-_")
    if not out:
        return text  # a label made only of a currency says nothing once emptied
    removed |= dropped
    return out


def _texts(chart: dict):
    """(holder, key) for each label the model wrote: title, axis titles, legend names, a card's label and text value."""
    for key in ("title", "xLabels", "yLabels", "label"):
        yield chart, key
    if chart.get("chartType") == "card":
        yield chart, "data"
    data = chart.get("data")
    if isinstance(data, dict) and isinstance(data.get("datasets"), list):
        for dataset in data["datasets"]:
            if isinstance(dataset, dict):
                yield dataset, "label"


def labelTexts(chart: object) -> list[str]:
    """Every label text of a chart the user is shown, in the order `removeUnsupportedCurrency` checks them."""
    if not isinstance(chart, dict):
        return []
    return [holder[key] for holder, key in _texts(chart) if isinstance(holder.get(key), str) and holder[key]]


def hasCurrency(chart: object) -> bool:
    """Whether any label of a chart names a currency (cheap: no evidence needed)."""
    if not isinstance(chart, dict):
        return False
    return any(isinstance(holder.get(key), str) and _TOKEN.search(holder[key]) for holder, key in _texts(chart))


def removeUnsupportedCurrency(chart: object, evidence: str, ref: object = None) -> None:
    """Remove from a chart's labels every currency the evidence does not name, in place. Never raises."""
    try:
        if not hasCurrency(chart):
            return
        evidenced, removed = _evidenced(evidence or ""), set()
        for holder, key in _texts(chart):
            if isinstance(holder.get(key), str):
                holder[key] = _clean(holder[key], evidenced, removed)
        if removed:
            # Tokens only: labels and values can hold customer data.
            logger.warning(f"chart_label_currency ref={ref} removed={sorted(removed)}")
    except Exception as error:  # a label check must never cost the user their chart
        logger.warning(f"chart_label_currency ref={ref} check failed: {type(error).__name__}")
