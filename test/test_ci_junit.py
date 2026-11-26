from pathlib import Path

import pytest

from scripts.check_ci_junit import validate_report


@pytest.fixture
def report(tmp_path: Path):
    def write(*cases: str) -> Path:
        path = tmp_path / "results.xml"
        path.write_text(
            f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>",
            encoding="utf-8",
        )
        return path

    return write


def test_accepts_an_executed_case(report):
    validate_report(report('<testcase classname="test.example" name="test_ran" />'))


def test_rejects_an_empty_report(report):
    with pytest.raises(ValueError, match="no test cases"):
        validate_report(report())


def test_rejects_an_unexpected_skip(report):
    path = report(
        '<testcase classname="test.example" name="test_ran" />',
        '<testcase classname="test.example" name="test_skipped"><skipped /></testcase>',
    )

    with pytest.raises(ValueError, match=r"unexpected skips: test\.example\.test_skipped"):
        validate_report(path, allowed_skips={"test.other.test_skipped"})


def test_accepts_a_named_skip_alongside_an_executed_case(report):
    path = report(
        '<testcase classname="test.example" name="test_ran" />',
        '<testcase classname="test.example" name="test_skipped"><skipped /></testcase>',
    )

    validate_report(path, allowed_skips={"test.example.test_skipped"})


def test_rejects_a_fully_skipped_suite_even_when_the_skip_is_allowed(report):
    path = report(
        '<testcase classname="test.example" name="test_skipped"><skipped /></testcase>',
    )

    with pytest.raises(ValueError, match="no executed test cases"):
        validate_report(path, allowed_skips={"test.example.test_skipped"})
