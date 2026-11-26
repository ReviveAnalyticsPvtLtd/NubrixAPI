"""Require executed JUnit cases and reject undocumented integration skips."""

import argparse
from collections.abc import Collection
from pathlib import Path
from xml.etree import ElementTree


def validate_report(path: Path, *, allowed_skips: Collection[str] = ()) -> None:
    cases = ElementTree.parse(path).findall(".//testcase")
    if not cases:
        raise ValueError("no test cases in the report")

    skipped_ids = [
        f"{case.get('classname')}.{case.get('name')}"
        for case in cases
        if case.find("skipped") is not None
    ]
    unexpected = sorted(set(skipped_ids).difference(allowed_skips))
    if unexpected:
        raise ValueError(f"unexpected skips: {', '.join(unexpected)}")
    if len(skipped_ids) == len(cases):
        raise ValueError("no executed test cases in the report")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--allow-skip", action="append", default=[], metavar="CLASSNAME.TEST")
    args = parser.parse_args()

    try:
        validate_report(args.report, allowed_skips=args.allow_skip)
    except (OSError, ElementTree.ParseError, ValueError) as error:
        parser.error(f"{args.report}: {error}")


if __name__ == "__main__":
    main()
