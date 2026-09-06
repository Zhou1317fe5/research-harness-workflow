from __future__ import annotations

import sys

import pytest

from remote_run_control.adapter import parse_adapter_result, run_adapter
from remote_run_control.errors import RRCError


def test_adapter_protocol_accepts_structured_verdict():
    value = {
        "protocol": "rrctl.adapter.v1",
        "healthy": True,
        "complete": False,
        "progress": {"completed": 1},
        "observations": {},
        "artifacts": [],
    }
    assert parse_adapter_result(value).healthy


@pytest.mark.parametrize(
    "value,code",
    [
        ({}, "adapter_protocol"),
        (
            {
                "protocol": "rrctl.adapter.v1",
                "healthy": False,
                "complete": True,
                "progress": {},
                "observations": {},
                "artifacts": [],
            },
            "adapter_verdict_inconsistent",
        ),
    ],
)
def test_adapter_protocol_rejects_invalid_verdict(value, code):
    with pytest.raises(RRCError) as caught:
        parse_adapter_result(value)
    assert caught.value.code == code


def test_adapter_process_errors_are_stable(tmp_path):
    malformed = tmp_path / "malformed.py"
    malformed.write_text("print('not-json')\n", encoding="utf-8")
    with pytest.raises(RRCError) as caught:
        run_adapter(
            (sys.executable, str(malformed)),
            {"phase": "periodic"},
            timeout_seconds=2,
            cwd=tmp_path,
        )
    assert caught.value.code == "adapter_json"

    timeout = tmp_path / "timeout.py"
    timeout.write_text("import time; time.sleep(5)\n", encoding="utf-8")
    with pytest.raises(RRCError) as caught:
        run_adapter(
            (sys.executable, str(timeout)),
            {},
            timeout_seconds=1,
            cwd=tmp_path,
        )
    assert caught.value.code == "adapter_timeout"

    secret = "sensitive-value"
    failing = tmp_path / "failing.py"
    failing.write_text(
        f"import sys; print('api_key={secret}', file=sys.stderr); raise SystemExit(2)\n",
        encoding="utf-8",
    )
    with pytest.raises(RRCError) as caught:
        run_adapter(
            (sys.executable, str(failing)),
            {},
            timeout_seconds=2,
            cwd=tmp_path,
        )
    assert secret not in caught.value.details["stderr"]


def test_adapter_receives_json_context(tmp_path):
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import json, sys\n"
        "context=json.load(sys.stdin)\n"
        "print(json.dumps({'protocol':'rrctl.adapter.v1','healthy':context['ok'],"
        "'complete':False,'progress':{},'observations':{},'artifacts':[]}))\n",
        encoding="utf-8",
    )
    result = run_adapter(
        (sys.executable, str(adapter)),
        {"ok": True},
        timeout_seconds=2,
        cwd=tmp_path,
    )
    assert result and result.healthy
