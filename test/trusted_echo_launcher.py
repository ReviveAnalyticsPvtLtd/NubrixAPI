"""
Test-only trusted launcher: reads its input file after enterSandbox and reports on it.

It is not in the production allow-list. A test adds it for one run with monkeypatch.

Modes (request["mode"]): "echo" writes the input as the result; "probe" writes a JSON report of what the
run could do to its input and what it was given; "hang" never finishes.
"""

import json
import os
import sys


def main():
    payload = json.load(sys.stdin)
    from utils.sandboxRunner import enterSandbox

    enterSandbox(payload["channel"])
    request = payload["request"]
    inputPath = payload["input_path"]
    mode = request.get("mode", "echo")
    if mode == "echo":
        with open(inputPath, "rb") as source:
            data = source.read()
        with open(payload["result_path"], "wb") as target:
            target.write(data)
        return
    if mode == "hang":
        import time

        time.sleep(600)
    scratch = os.path.join(os.path.dirname(payload["result_path"]), "scratch")
    open(scratch, "wb").close()
    report = {"uid": os.getuid() if hasattr(os, "getuid") else -1, "request": request, "payloadKeys": sorted(payload)}
    info = os.stat(inputPath)
    report.update(inputUid=info.st_uid, inputGid=info.st_gid, tables=sorted(os.listdir(os.path.dirname(inputPath))), inputMode=oct(info.st_mode & 0o777), writable=os.access(inputPath, os.W_OK))
    for name, attempt in (
        ("append", lambda: open(inputPath, "ab").close()),
        ("replace", lambda: os.replace(scratch, inputPath)),
        ("unlink", lambda: os.unlink(inputPath)),
        ("createBeside", lambda: open(os.path.join(os.path.dirname(inputPath), "other"), "wb").close()),
    ):
        try:
            attempt()
            report[name] = "allowed"
        except OSError as error:
            report[name] = type(error).__name__
    if sys.platform == "linux":
        import resource
        import socket

        report["nproc"] = resource.getrlimit(resource.RLIMIT_NPROC)[0]
        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            report["network"] = "allowed"
        except OSError as error:
            report["network"] = type(error).__name__
    try:
        from utils.initMethods import fetch_data

        fetch_data(payload["projectId"], "anything")
        report["tableRead"] = "allowed"
    except Exception as error:
        report["tableRead"] = str(error)
    # Everything the run holds that could carry a secret.
    report["seen"] = json.dumps(payload) + json.dumps(dict(os.environ))
    with open(payload["result_path"], "w") as target:
        json.dump(report, target)


if __name__ == "__main__":
    main()
