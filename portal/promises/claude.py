"""Running ``claude -p`` headless, cheaply and contained.

Follows what was measured in HEADLESS-CLAUDE-COSTS: a headless call
inherits the caller's whole Claude Code setup unless told otherwise --
settings (effort xhigh), plugins, hooks, CLAUDE.md and auto-memory from
the working directory -- and for a short call that can cost several
times the work. So every call:

* passes ``--tools`` (none for classification; Read/Grep/Glob to judge),
  ``--setting-sources project,local`` and an explicit ``--effort``;
* runs from a directory with no CLAUDE.md above it, with its own HOME;
* gets an environment built from scratch -- never a copy of the
  service's, which holds the Gerrit password and the session key;
* is capped by ``--max-budget-usd``;
* is first in line for the kernel's OOM killer, so a squeeze on a small
  host kills this call rather than the web server next to it.

Permissions are NOT skipped: in ``-p`` mode a read outside the working
directory -- including through a symlink -- is refused, which is what
keeps comment text (untrusted input) from steering the judge to
anything but the snapshot it was given.

Calls run in parallel up to a cap, and only while enough memory is free
(see :class:`Gate`): each call is a ~200 MB process, and on a small host
the web server next to it matters more. A call can be cancelled; its
process group is killed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

_LIMIT_RE = re.compile(
    r"usage limit|hit your limit|limit will reset|rate[_ ]limit|quota", re.IGNORECASE
)

#: Passed through from the service's environment when set, and nothing else.
_PASS_THROUGH = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "LANG",
    "TZ",
)


class ClaudeError(RuntimeError):
    pass


class QuotaReached(ClaudeError):
    """The account's usage limit was hit: stop starting calls."""


class Cancelled(ClaudeError):
    """Someone pressed cancel."""


def mem_available_mb() -> int | None:
    """MemAvailable from /proc/meminfo, or None where there is none."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class Gate:
    """At most ``max_parallel`` calls at once, and a new one only while at
    least ``min_free_mb`` of memory is available -- so the cap can be
    generous and a small host still only runs what fits."""

    def __init__(self, max_parallel: int = 10, min_free_mb: int = 400, meminfo=mem_available_mb):
        self.max_parallel = max(1, max_parallel)
        self.min_free_mb = min_free_mb
        self._meminfo = meminfo
        self._lock = threading.Lock()
        self._running = 0

    def enter(self, cancel: threading.Event | None = None, poll: float = 2.0) -> None:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled before it started")
            with self._lock:
                free = self._meminfo()
                # One call may always run: waiting for memory that never
                # frees up would stall the job for good.
                fits = self._running == 0 or free is None or free >= self.min_free_mb
                if self._running < self.max_parallel and fits:
                    self._running += 1
                    return
            time.sleep(poll)

    def leave(self) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)

    @property
    def running(self) -> int:
        return self._running


#: The process-wide gate; its limits come from the app settings.
GATE = Gate()


def configure(max_parallel: int, min_free_mb: int) -> None:
    GATE.max_parallel = max(1, max_parallel)
    GATE.min_free_mb = min_free_mb


@dataclass
class Result:
    """``usd`` is what Claude Code reports as the call's cost: the API
    price. On a subscription nothing is charged -- it is a measure of how
    much of the plan's usage the call took."""

    text: str
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    model: str = ""
    denials: list = field(default_factory=list)

    def usage(self) -> dict:
        return {
            "usd": self.usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "turns": self.turns,
            "model": self.model,
        }


def find_binary(configured: str | None = None) -> str | None:
    for candidate in (configured, os.environ.get("CLAUDE_BINARY")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("claude")


def has_credentials() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))


def child_env(home: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        # No update checks, telemetry or other traffic that is not the call.
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    for key in _PASS_THROUGH:
        if os.environ.get(key):
            env[key] = os.environ[key]
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        # A subscription token is set: never let an API key take over,
        # which would bill per token instead of counting against the plan.
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def command(
    binary: str, *, model: str, effort: str, tools: str, budget_usd: float, system_prompt: str
) -> list[str]:
    cmd = [
        binary,
        "-p",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--setting-sources",
        "project,local",
        "--disable-slash-commands",
        "--tools",
        tools,
        "--effort",
        effort,
        "--max-budget-usd",
        f"{budget_usd:.2f}",
        "--system-prompt",
        system_prompt,
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def _deprioritise():
    """Runs in the child before exec: be the OOM killer's first choice.
    Raising one's own score needs no privilege."""
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass


def run(
    prompt: str,
    *,
    binary: str,
    home: str,
    cwd: str,
    model: str,
    effort: str,
    tools: str,
    budget_usd: float,
    timeout: int,
    system_prompt: str,
    cancel: threading.Event | None = None,
    gate: Gate | None = None,
) -> Result:
    """One call. Raises ClaudeError: QuotaReached for the usage limit;
    Cancelled when ``cancel`` is set, before the call starts or while it
    runs (then its process group is killed)."""
    os.makedirs(home, exist_ok=True)
    cmd = command(
        binary,
        model=model,
        effort=effort,
        tools=tools,
        budget_usd=budget_usd,
        system_prompt=system_prompt,
    )
    gate = gate or GATE
    gate.enter(cancel)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=child_env(home),
            start_new_session=True,  # its own process group: a kill takes all of it
            preexec_fn=_deprioritise,
        )
        out, err = _communicate(proc, prompt, timeout, cancel)
    finally:
        gate.leave()

    envelope = None
    try:
        envelope = json.loads((out or "").strip())
    except json.JSONDecodeError:
        pass
    if not isinstance(envelope, dict):
        blob = f"{out or ''}\n{err or ''}"
        if _LIMIT_RE.search(blob):
            raise QuotaReached("the Claude usage limit was reached")
        raise ClaudeError(f"claude exited {proc.returncode}: {(err or out or '').strip()[:300]}")

    text = str(envelope.get("result") or "")
    usage = envelope.get("usage") or {}
    result = Result(
        text=text,
        usd=float(envelope.get("total_cost_usd") or 0.0),
        input_tokens=int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        turns=int(envelope.get("num_turns") or 0),
        model=model,
        denials=envelope.get("permission_denials") or [],
    )
    if envelope.get("is_error"):
        if _LIMIT_RE.search(text):
            raise QuotaReached("the Claude usage limit was reached")
        subtype = envelope.get("subtype") or "error"
        err = ClaudeError(f"claude reported {subtype}: {text[:300]}")
        err.result = result  # what it cost is still worth recording
        raise err
    return result


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


# The exception communicate() raises when a step times out. Under eventlet
# (the web service) ``subprocess`` is eventlet's module, whose
# TimeoutExpired is not the class Popen.communicate -- inherited from the
# standard library -- actually raises; catching only
# ``subprocess.TimeoutExpired`` let every step's timeout escape.
_STEP_TIMEOUT = tuple(
    {
        subprocess.TimeoutExpired,
        subprocess.Popen.communicate.__globals__.get("TimeoutExpired", subprocess.TimeoutExpired),
    }
)


def _communicate(
    proc, prompt: str, timeout: int, cancel: threading.Event | None, step: float = 2.0
):
    """communicate(), in short steps so a cancel is noticed within seconds.
    Retrying after TimeoutExpired loses no output (subprocess docs)."""
    deadline = time.monotonic() + timeout
    first = True
    while True:
        try:
            return proc.communicate(
                input=prompt if first else None,
                timeout=min(step, max(0.1, deadline - time.monotonic())),
            )
        except _STEP_TIMEOUT:
            first = False
            if cancel is not None and cancel.is_set():
                _kill(proc)
                raise Cancelled("cancelled") from None
            if time.monotonic() >= deadline:
                _kill(proc)
                raise ClaudeError(f"claude timed out after {timeout}s") from None


def extract_json(text: str, required_key: str) -> dict | None:
    """The whole text as JSON, else the first balanced object that has
    ``required_key`` (string- and escape-aware)."""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and required_key in obj:
            return obj
    except json.JSONDecodeError:
        pass
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(text[start : i + 1])
                    if isinstance(obj, dict) and required_key in obj:
                        return obj
                except json.JSONDecodeError:
                    pass
                start = None
    return None
