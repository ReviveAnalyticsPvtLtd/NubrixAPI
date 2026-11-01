"""Tests that a failed sandbox run never reads as a successful one."""

import json
from unittest import mock

from nubrix.components import transformationExecutor as executorModule
from utils import codeExecutor as codeExecutorModule


def _inspect(stdout, stderr, returnCode):
    executor = object.__new__(executorModule.TransformationExecutor)
    executor.timeoutSeconds = 0
    with mock.patch.object(executorModule, "_run_subprocess", return_value=(stdout, stderr, returnCode)):
        return executor.executeInspection("project_1", "print(1)")


def test_inspectionKilledWithNoOutputIsReportedAsAFailure():
    result = _inspect("", "", -9)

    assert "successfully" not in result
    assert "-9" in result


def test_inspectionKilledAfterPartialOutputKeepsTheOutputAndTheFailure():
    result = _inspect("first rows", "", -9)

    assert "first rows" in result
    assert "-9" in result


def test_inspectionFailureWithErrorsStillShowsThem():
    result = _inspect("first rows", "Traceback: boom", 1)

    assert "first rows" in result
    assert "Traceback: boom" in result


def test_inspectionWithOutputReturnsItUnchanged():
    assert _inspect("first rows", "", 0) == "first rows"


def test_inspectionWithNoOutputStillReportsSuccess():
    assert _inspect("", "", 0) == "Inspection executed successfully with no output."


class _FakeRedis:
    def zremrangebyscore(self, *_args):
        return 0

    def zcard(self, _key):
        return 0

    def zadd(self, *_args):
        return 1

    def expire(self, *_args):
        return True

    def zrem(self, *_args):
        return 1


def _run(popenResult):
    manager = codeExecutorModule.REPLManager(timeoutSeconds=7)
    with (
        mock.patch.object(codeExecutorModule.creditService, "_redis", return_value=_FakeRedis()),
        mock.patch.object(codeExecutorModule.subprocess, "Popen", **popenResult),
    ):
        return manager.run("print(1)", projectId="project_1")


def _finished(stdout, stderr, returnCode):
    process = mock.Mock(returncode=returnCode)
    process.communicate.return_value = (stdout, stderr)
    return {"return_value": process}


def _isJson(text):
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def test_failedRunNeverReturnsItsPartialOutput():
    assert _run(_finished('{"data": [1, 2]}', "Traceback: boom", 1)) == "Traceback: boom"


def test_killedRunWithNoErrorsIsNotValidOutput():
    result = _run(_finished('{"data": [1, 2]}', "", -9))

    assert not _isJson(result)
    assert "-9" in result


def test_killedRunWithNoOutputStillExplainsItself():
    assert "-9" in _run(_finished("", "", -9))


def test_successfulRunReturnsItsOutput():
    assert _run(_finished('{"data": [1, 2]}', "a warning on stderr", 0)) == '{"data": [1, 2]}'


def test_successfulRunWithNoOutputReturnsItsStderr():
    assert _run(_finished("", "a warning on stderr", 0)) == "a warning on stderr"


def test_launchFailureIsReported():
    assert "no such launcher" in _run({"side_effect": OSError("no such launcher")})
