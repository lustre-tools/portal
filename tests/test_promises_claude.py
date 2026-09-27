"""The headless Claude runner: lean, contained, and honest about cost."""

import json
import os
import stat

import pytest

from portal.promises import claude


def fake_claude(tmp_path, body):
    """A stand-in `claude` that runs a shell body with the prompt on stdin."""
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def run(binary, tmp_path, **kw):
    args = {
        "binary": binary,
        "home": str(tmp_path / "home"),
        "cwd": str(tmp_path),
        "model": "sonnet",
        "effort": "medium",
        "tools": "",
        "budget_usd": 0.5,
        "timeout": 20,
        "system_prompt": "answer in JSON",
    }
    args.update(kw)
    args.setdefault("gate", claude.Gate(min_free_mb=0))
    return claude.run("the prompt", **args)


def test_the_command_is_the_lean_one():
    """Measured in HEADLESS-CLAUDE-COSTS: without these, a call inherits
    settings (effort xhigh), plugins and hooks, at several times the cost."""
    cmd = claude.command(
        "claude",
        model="opus",
        effort="medium",
        tools="Read,Grep,Glob",
        budget_usd=2,
        system_prompt="S",
    )
    for flag in ("-p", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local"
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "2.00"
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert "--dangerously-skip-permissions" not in cmd, (
        "the judge must stay confined to its snapshot"
    )


def test_the_child_gets_no_secret_it_does_not_need(monkeypatch, tmp_path):
    monkeypatch.setenv("GERRIT_PASS", "hunter2")
    monkeypatch.setenv("PORTAL_SECRET_KEY", "k")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer-session")
    env = claude.child_env(str(tmp_path))
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["HOME"] == str(tmp_path)
    for leaked in ("GERRIT_PASS", "PORTAL_SECRET_KEY", "CLAUDE_CODE_SESSION_ID"):
        assert leaked not in env


def test_a_result_carries_its_cost(tmp_path):
    envelope = {
        "result": '{"ok": 1}',
        "total_cost_usd": 0.042,
        "num_turns": 3,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7},
    }
    binary = fake_claude(tmp_path, f"cat >/dev/null; echo '{json.dumps(envelope)}'")
    res = run(binary, tmp_path)
    assert res.text == '{"ok": 1}'
    assert (res.usd, res.turns, res.input_tokens, res.output_tokens) == (0.042, 3, 15, 7)


def test_it_runs_in_the_given_directory_with_its_own_home(tmp_path):
    binary = fake_claude(
        tmp_path, 'cat >/dev/null; printf \'{"result": "%s|%s"}\' "$(pwd)" "$HOME"'
    )
    (tmp_path / "work").mkdir()
    res = run(binary, tmp_path, cwd=str(tmp_path / "work"))
    cwd, home = res.text.split("|")
    assert os.path.realpath(cwd) == os.path.realpath(tmp_path / "work")
    assert home == str(tmp_path / "home")


def test_the_usage_limit_stops_the_run(tmp_path):
    binary = fake_claude(
        tmp_path,
        "cat >/dev/null; echo 'Claude usage limit reached. Your limit will reset at 5pm' >&2; exit 1",
    )
    with pytest.raises(claude.QuotaReached):
        run(binary, tmp_path)


def test_an_error_result_still_reports_what_it_cost(tmp_path):
    envelope = {
        "is_error": True,
        "subtype": "error_max_budget_usd",
        "result": "",
        "total_cost_usd": 0.5,
    }
    binary = fake_claude(tmp_path, f"cat >/dev/null; echo '{json.dumps(envelope)}'")
    with pytest.raises(claude.ClaudeError) as err:
        run(binary, tmp_path)
    assert err.value.result.usd == 0.5
    assert "error_max_budget_usd" in str(err.value)


def test_a_hung_call_is_killed(tmp_path):
    binary = fake_claude(tmp_path, "sleep 30")
    with pytest.raises(claude.ClaudeError, match="timed out"):
        run(binary, tmp_path, timeout=1)


def test_json_is_found_inside_prose():
    text = 'Here you go:\n```json\n{"verdict": "addressed", "note": "a } in a string"}\n```'
    assert claude.extract_json(text, "verdict")["note"] == "a } in a string"
    assert claude.extract_json("no json here", "verdict") is None


def test_a_subscription_token_is_never_overridden_by_an_api_key(monkeypatch, tmp_path):
    """With both set, Claude Code could bill per token."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-key")
    env = claude.child_env(str(tmp_path))
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok" and "ANTHROPIC_API_KEY" not in env
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    assert claude.child_env(str(tmp_path))["ANTHROPIC_API_KEY"] == "sk-key"


# ---------- parallel calls and cancelling ----------

import threading  # noqa: E402
import time  # noqa: E402


def test_the_gate_caps_parallel_calls():
    gate = claude.Gate(max_parallel=2, min_free_mb=0, meminfo=lambda: 10_000)
    gate.enter()
    gate.enter()
    started = threading.Event()

    def third():
        gate.enter(poll=0.01)
        started.set()

    threading.Thread(target=third, daemon=True).start()
    assert not started.wait(0.2), "a third call must wait"
    gate.leave()
    assert started.wait(1)


def test_the_gate_waits_for_memory_but_never_stalls_alone():
    free = [100]
    gate = claude.Gate(max_parallel=10, min_free_mb=400, meminfo=lambda: free[0])
    gate.enter()  # the first call always runs, or a busy host would stall for good
    started = threading.Event()
    threading.Thread(target=lambda: (gate.enter(poll=0.01), started.set()), daemon=True).start()
    assert not started.wait(0.2), "no second call while memory is short"
    free[0] = 900
    assert started.wait(1)


def test_a_cancel_while_waiting_raises():
    gate = claude.Gate(max_parallel=1, min_free_mb=0, meminfo=lambda: None)
    gate.enter()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(claude.Cancelled):
        gate.enter(cancel, poll=0.01)


def test_a_cancel_kills_a_running_call(tmp_path):
    binary = fake_claude(tmp_path, "sleep 60")
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(claude.Cancelled):
        run(binary, tmp_path, cancel=cancel, timeout=60, gate=claude.Gate(min_free_mb=0))
    assert time.monotonic() - started < 10


def test_calls_longer_than_a_step_work_under_eventlet(tmp_path):
    """The web service runs monkey-patched by eventlet. There, the
    TimeoutExpired a communicate() step raises is not eventlet's class,
    and catching the wrong one failed every call longer than two seconds.
    Run it the way the service does, in a process of its own."""
    import subprocess
    import sys

    slow = fake_claude(tmp_path, 'cat >/dev/null; sleep 3; echo \'{"result": "slow but fine"}\'')
    (tmp_path / "h").mkdir()
    hung = fake_claude(tmp_path / "h", "sleep 60")
    script = tmp_path / "run.py"
    script.write_text(
        "import eventlet\n"
        "eventlet.monkey_patch()\n"
        "import sys\n"
        "from portal.promises import claude\n"
        "kw = dict(home=sys.argv[3], cwd=sys.argv[3], model='m', effort='low', tools='', budget_usd=0.1,\n"
        "          system_prompt='s', gate=claude.Gate(min_free_mb=0))\n"
        "print(claude.run('p', binary=sys.argv[1], timeout=30, **kw).text)\n"
        "try:\n"
        "    claude.run('p', binary=sys.argv[2], timeout=3, **kw)\n"
        "except claude.ClaudeError as exc:\n"
        "    print('hung:', exc)\n"
    )
    proc = subprocess.run(
        [sys.executable, str(script), slow, hung, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "slow but fine" in proc.stdout
    assert "hung: claude timed out after 3s" in proc.stdout
