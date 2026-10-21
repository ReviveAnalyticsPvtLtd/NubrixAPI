"""The manual-capture path is gone: every Order auto-captures, so no code may reference it."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOVED = re.compile(r'reserveCapture|beginCaptureSubmission|requireManualCaptureCurrency|'
                     r'SUBSCRIPTION_MANUAL_CAPTURE_CURRENCIES|purchaseCaptureService|PurchaseCaptureService')


def test_no_manual_capture_symbols_remain():
    offenders = []
    for folder in ('api', 'nubrix'):
        for path in (ROOT / folder).rglob('*.py'):
            if '__pycache__' in path.parts:
                continue
            text = path.read_text(encoding='utf-8')
            if REMOVED.search(text):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders
