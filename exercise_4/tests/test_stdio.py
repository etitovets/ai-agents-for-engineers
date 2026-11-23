"""End-to-end stdio checks: stdout carries MCP frames and nothing else."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import PROJECT_ROOT, RUNBOOKS_DIR

EXPECTED_TOOLS = {
    "incidents_search",
    "incident_get",
    "deploys_recent",
    "logs_query",
    "metrics_latency",
    "runbook_get",
    "service_catalog_get",
}


def rpc(method: str, *, params: dict | None = None, request_id: int | None = None) -> str:
    message: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    return json.dumps(message)


def run_server(
    payload: str, env: dict[str, str], *, cwd: Path = PROJECT_ROOT, timeout: float = 60.0
) -> tuple[int, str, str]:
    process = subprocess.Popen(
        [sys.executable, "-m", "oncall_mcp"],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        pytest.fail("server did not exit when stdin closed; stderr tail:\n" + stderr[-2000:])
    return process.returncode, stdout, stderr


def test_stdio_stdout_is_only_json_rpc(test_dsn: str) -> None:
    env = {
        **os.environ,
        "STAND_DATABASE_URL": test_dsn,
        "ONCALL_RUNBOOKS_DIR": str(RUNBOOKS_DIR),
        "PYTHONUNBUFFERED": "1",
    }
    messages = [
        rpc(
            "initialize",
            params={
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
            request_id=1,
        ),
        rpc("notifications/initialized"),
        rpc("tools/list", params={}, request_id=2),
        rpc(
            "tools/call",
            params={"name": "service_catalog_get", "arguments": {"service": "payments"}},
            request_id=3,
        ),
        rpc(
            "tools/call",
            params={"name": "runbook_get", "arguments": {"service": "../../etc/passwd"}},
            request_id=4,
        ),
    ]

    process = subprocess.Popen(
        [sys.executable, "-m", "oncall_mcp"],
        cwd=PROJECT_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin and process.stdout and process.stderr

    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    # Keep stdin open until every response has arrived: closing it makes the
    # server shut down and can race with the last reply.
    responses: dict[int, dict] = {}
    deadline = time.monotonic() + 60
    try:
        for message in messages:
            process.stdin.write(message + "\n")
            process.stdin.flush()

        while len(responses) < 4 and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=max(deadline - time.monotonic(), 0.1))
            except queue.Empty:
                break
            if line is None:
                break
            frame = json.loads(line)  # any non-JSON line fails here
            assert frame.get("jsonrpc") == "2.0"
            assert "FastMCP" not in line
            if "id" in frame:
                responses[frame["id"]] = frame
    finally:
        process.stdin.close()
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
        stderr = process.stderr.read()

    assert set(responses) == {1, 2, 3, 4}
    assert code == 0, stderr[-2000:]

    assert responses[1]["result"]["serverInfo"]["name"] == "oncall"
    tool_names = {tool["name"] for tool in responses[2]["result"]["tools"]}
    assert tool_names == EXPECTED_TOOLS

    success = responses[3]["result"]
    assert success.get("isError", False) is False
    assert success["structuredContent"]["item"]["name"] == "payments"

    failure = responses[4]["result"]
    assert failure["isError"] is True
    assert "invalid_arguments" in failure["content"][0]["text"]

    # Diagnostics go to stderr, never stdout, and never include the DSN.
    assert "postgres pool opened" in stderr
    assert test_dsn not in stderr


def test_invalid_dsn_stops_with_a_controlled_error(tmp_path: Path) -> None:
    env = {**os.environ, "STAND_DATABASE_URL": "not-a-dsn"}
    env.pop("ONCALL_RUNBOOKS_DIR", None)

    code, stdout, stderr = run_server("", env, cwd=tmp_path, timeout=30.0)

    assert code == 2
    assert stdout == ""
    assert "configuration error" in stderr
    assert "STAND_DATABASE_URL" in stderr
    assert "not-a-dsn" not in stderr
    assert "Traceback" not in stderr
