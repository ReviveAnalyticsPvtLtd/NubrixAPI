import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("token", ["", "test-only-token"])
def test_logtail_sink_requires_a_configured_token(tmp_path, token):
    program = """
import logging
import os
from unittest.mock import patch

with patch('logtail.LogtailHandler', return_value=logging.NullHandler()) as handler:
    import utils.logger

    token = os.environ['LOGTAIL_TOKEN']
    if token:
        assert handler.call_args.kwargs['source_token'] == token
    else:
        handler.assert_not_called()
"""
    subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env=os.environ | {
            "LOGTAIL_TOKEN": token,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        },
        check=True,
    )
