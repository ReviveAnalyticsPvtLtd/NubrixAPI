"""Fill hover labels in JSON chart data returned to map clients."""

from math import isfinite


def _labelText(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (bool, int)):
        return str(value)
    if isinstance(value, float) and isfinite(value):
        return "0" if value == 0 else str(value)
    return ""


def ensureScatterMapPointLabels(chart: object) -> None:
    """Fill the exact point-level label key on a parsed scatter-map response."""
    if not isinstance(chart, dict) or chart.get("chartType") not in ("geoMap", "geo-map"):
        return
    map_data = chart.get("map")
    if not isinstance(map_data, dict) or map_data.get("mapType") != "scatterMap":
        return
    data = map_data.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("points"), list):
        return

    for point in data["points"]:
        if not isinstance(point, dict):
            continue
        label = _labelText(point.get("label"))
        properties = point.get("properties")
        if not label and isinstance(properties, dict):
            label = _labelText(properties.get("city"))
        if not label:
            lat = _labelText(point.get("lat"))
            lon = _labelText(point.get("long"))
            label = f"{lat}, {lon}" if lat and lon else _labelText(point.get("id"))
        point["label"] = label or "Unknown location"
