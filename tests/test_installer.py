"""The installer, run for real in its non-destructive modes.

--render-site prints the nginx site and changes nothing; --dry-run prints
every action and takes none. Both are enough to check the two things that
matter most on a live host: that a re-run rebuilds the SAME site from the
remembered answers, and where the gc CLI is installed from.
"""

import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.sh")


def run(args, config_dir, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PORTAL_", "GERRIT_"))}
    env["PORTAL_CONFIG_DIR"] = str(config_dir)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", INSTALL, *args], env=env, capture_output=True, text=True, timeout=120
    )


def remember(config_dir, **answers):
    lines = [f'{k}="{v}"' for k, v in answers.items()]
    (config_dir / "install.conf").write_text("\n".join(lines) + "\n")


# ---------- remembered answers ----------


def test_render_site_reuses_the_remembered_answers(tmp_path):
    """A plain re-run used to take every answer from the environment or a
    default -- so the hostname came from `hostname -f` and a live site
    could be rebuilt for the wrong name."""
    remember(tmp_path, SERVER_NAME="saved.example.org", PORT="5123", ACME_ROOT="/srv/acme")
    r = run(["--render-site"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "server_name saved.example.org;" in r.stdout
    assert "127.0.0.1:5123" in r.stdout
    assert "root /srv/acme;" in r.stdout
    assert "/etc/letsencrypt/live/saved.example.org/fullchain.pem" in r.stdout


def test_environment_overrides_a_remembered_answer(tmp_path):
    remember(tmp_path, SERVER_NAME="saved.example.org", PORT="5123")
    r = run(["--render-site"], tmp_path, {"PORTAL_SERVER_NAME": "env.example.org"})
    assert r.returncode == 0, r.stderr
    assert "server_name env.example.org;" in r.stdout
    assert "saved.example.org" not in r.stdout
    assert "127.0.0.1:5123" in r.stdout, "the other remembered answers still apply"


def test_a_new_hostname_gets_its_own_certificate(tmp_path):
    """The default cert path follows the hostname; a remembered default
    would have kept loading the old name's certificate."""
    remember(tmp_path, SERVER_NAME="old.example.org")
    r = run(["--render-site"], tmp_path, {"PORTAL_SERVER_NAME": "new.example.org"})
    assert "/etc/letsencrypt/live/new.example.org/" in r.stdout
    assert "old.example.org" not in r.stdout


def test_render_site_refuses_without_a_hostname(tmp_path):
    r = run(["--render-site"], tmp_path)
    assert r.returncode != 0
    assert "No hostname" in r.stderr


def test_render_site_matches_the_template_exactly(tmp_path):
    """--render-site must be the file an install writes, or reviewing it
    before an upgrade proves nothing."""
    remember(tmp_path, SERVER_NAME="x.example.org", PORT="5000")
    r = run(["--render-site"], tmp_path)
    assert "@" not in "".join(
        line for line in r.stdout.splitlines() if not line.lstrip().startswith("#")
    ), "an unsubstituted placeholder reached the rendered site"


# ---------- where the gc CLI comes from ----------


@pytest.fixture
def tools_checkout(tmp_path):
    d = tmp_path / "llm_code_and_review_tools"
    for pkg in ("llm_tool_common", "gerrit_cli", "gerrit_dashboard"):
        (d / pkg).mkdir(parents=True)
        (d / pkg / "pyproject.toml").write_text("[project]\nname='x'\n")
    return d


def pip_lines(output):
    return [line for line in output.splitlines() if "would run:" in line and "pip install" in line]


def test_bundled_tools_are_the_default(tmp_path):
    r = run(["--dry-run", "--yes"], tmp_path / "cfg")
    assert r.returncode == 0, r.stderr[-2000:]
    lines = pip_lines(r.stdout)
    assert any("-e /opt/portal/vendor/llm_tools/gerrit_cli" in line for line in lines), lines
    assert "would write: " + str(tmp_path / "cfg" / "install.conf") in r.stdout, (
        "a completed install must record its answers for the next run"
    )


def test_a_tools_checkout_is_installed_as_a_copy(tmp_path, tools_checkout):
    """Not editable: the service cannot read a checkout under /root at run
    time, so an editable install would fail on the first import."""
    r = run(["--dry-run", "--yes"], tmp_path / "cfg", {"PORTAL_TOOLS_DIR": str(tools_checkout)})
    assert r.returncode == 0, r.stderr[-2000:]
    lines = pip_lines(r.stdout)
    gc = [line for line in lines if line.rstrip().endswith(f"{tools_checkout}/gerrit_cli")]
    assert gc, lines
    assert any("--no-deps --force-reinstall" in line for line in gc), (
        "must replace whatever was there"
    )
    assert not any("-e " in line and str(tools_checkout) in line for line in lines), (
        "must not be editable"
    )
    assert not any("vendor/llm_tools/gerrit_cli" in line for line in lines)


def test_a_remembered_tools_dir_is_reused(tmp_path, tools_checkout):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    remember(cfg, TOOLS_DIR=str(tools_checkout))
    r = run(["--dry-run", "--yes"], cfg)
    assert any(str(tools_checkout) in line for line in pip_lines(r.stdout))


def test_bundled_switches_back(tmp_path, tools_checkout):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    remember(cfg, TOOLS_DIR=str(tools_checkout))
    r = run(["--dry-run", "--yes"], cfg, {"PORTAL_TOOLS_DIR": "bundled"})
    lines = pip_lines(r.stdout)
    assert any("vendor/llm_tools/gerrit_cli" in line for line in lines)
    assert not any(str(tools_checkout) in line for line in lines)


def test_a_bad_tools_dir_is_refused(tmp_path):
    r = run(["--dry-run", "--yes"], tmp_path / "cfg", {"PORTAL_TOOLS_DIR": str(tmp_path)})
    assert r.returncode != 0
    assert "not an llm_code_and_review_tools checkout" in r.stderr


# ---------- the dashboard choice ----------


def _installs_dashboard(output):
    return "portal-dashboard.service" in output


def test_the_dashboard_can_be_declined_non_interactively(tmp_path):
    """--yes accepts every prompt. A host that already runs a dashboard
    must be able to say no, or it gets a second one on the same port."""
    r = run(["--dry-run", "--yes"], tmp_path / "cfg", {"PORTAL_WITH_DASHBOARD": "0"})
    assert r.returncode == 0, r.stderr[-2000:]
    assert not _installs_dashboard(r.stdout)


def test_the_dashboard_can_be_accepted_non_interactively(tmp_path):
    r = run(["--dry-run", "--yes"], tmp_path / "cfg", {"PORTAL_WITH_DASHBOARD": "1"})
    assert _installs_dashboard(r.stdout)


def test_a_declined_dashboard_stays_declined_on_a_re_run(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    remember(cfg, WITH_DASHBOARD="0")
    r = run(["--dry-run", "--yes"], cfg)
    assert not _installs_dashboard(r.stdout)


# ---------- a second instance on the same host ----------


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def written_units(output):
    output = ANSI.sub("", output)
    return sorted(
        line.split("would write:")[1].split("(")[0].strip()
        for line in output.splitlines()
        if "would write:" in line and "/etc/systemd/system/" in line
    )


def test_the_default_instance_keeps_the_plain_names(tmp_path):
    """Live runs on these names; an upgrade must never rename them."""
    r = run(["--dry-run", "--yes"], tmp_path / "cfg", {"PORTAL_WITH_DASHBOARD": "0"})
    assert r.returncode == 0, r.stderr[-2000:]
    assert written_units(r.stdout) == [
        "/etc/systemd/system/portal-refresh.service",
        "/etc/systemd/system/portal-refresh.timer",
        "/etc/systemd/system/portal.service",
    ]
    assert "/opt/portal/.venv" in r.stdout


def test_a_named_instance_shares_nothing(tmp_path):
    """Staging beside production: every unit, directory and the service
    user carry the instance name."""
    env = {"PORTAL_INSTANCE": "staging", "PORTAL_WITH_DASHBOARD": "0"}
    r = run(["--dry-run", "--yes"], tmp_path / "cfg", env)
    assert r.returncode == 0, r.stderr[-2000:]
    assert written_units(r.stdout) == [
        "/etc/systemd/system/portal-staging-refresh.service",
        "/etc/systemd/system/portal-staging-refresh.timer",
        "/etc/systemd/system/portal-staging.service",
    ]
    assert "/opt/portal-staging/.venv" in r.stdout
    assert "useradd --system --home-dir /var/lib/portal-staging" in r.stdout
    assert "portal-staging" in r.stdout.split("useradd")[1].splitlines()[0]
    assert " /opt/portal/" not in r.stdout and "/opt/portal/.venv" not in r.stdout


def test_a_named_instance_has_its_own_site_extra(tmp_path):
    """The drop-ins proxy to one instance's port and gate on its sessions.
    Shared, a staging site would check logins against production."""
    remember(tmp_path, SERVER_NAME="stage.example.org", PORT="5200")
    r = run(["--render-site"], tmp_path, {"PORTAL_INSTANCE": "staging"})
    assert r.returncode == 0, r.stderr
    assert "include /etc/nginx/portal/site-extra-staging/*.conf;" in r.stdout
    assert "site-extra/*.conf" not in r.stdout


def test_a_bad_instance_name_is_refused(tmp_path):
    r = run(["--render-site"], tmp_path, {"PORTAL_INSTANCE": "Bad Name"})
    assert r.returncode != 0
    assert "PORTAL_INSTANCE must be" in r.stderr


def test_an_upgrade_restarts_the_service(tmp_path):
    """enable --now does nothing to a running unit, so an upgrade used to
    leave the old process serving -- measured: 25 minutes older than its
    own code. It must restart, for whichever instance this is."""
    for env, unit in (
        ({}, "portal.service"),
        ({"PORTAL_INSTANCE": "staging"}, "portal-staging.service"),
    ):
        r = run(["--dry-run", "--yes"], tmp_path / unit, {"PORTAL_WITH_DASHBOARD": "0", **env})
        out = ANSI.sub("", r.stdout)
        assert f"would run: systemctl restart {unit}" in out, unit
        assert f"enable --now {unit}" not in out


# ---------- the refresh timer and the stats backfill ----------


def _install(tmp_path, **env):
    r = run(["--dry-run", "--yes"], tmp_path, {"PORTAL_WITH_DASHBOARD": "0", **env})
    assert r.returncode == 0, r.stderr[-2000:]
    return ANSI.sub("", r.stdout)


def test_the_refresh_timer_is_on_by_default(tmp_path):
    out = _install(tmp_path)
    assert "would run: systemctl enable portal-refresh.timer" in out
    assert "would run: systemctl restart portal-refresh.timer" in out


def test_the_refresh_timer_can_be_left_off_and_that_is_remembered(tmp_path):
    """A staging copy beside production must not regenerate every graph a
    second time. The installer used to switch the timer back on at every
    upgrade, however often it was disabled."""
    out = _install(tmp_path, PORTAL_INSTANCE="staging", PORTAL_REFRESH_TIMER="0")
    assert "would run: systemctl disable --now portal-staging-refresh.timer" in out
    assert "systemctl enable portal-staging-refresh.timer" not in out
    assert "restart portal-staging-refresh.timer" not in out

    # What --dry-run would have remembered; a plain re-run must honour it.
    remember(tmp_path, REFRESH_TIMER="0")
    out = _install(tmp_path, PORTAL_INSTANCE="staging")
    assert "restart portal-staging-refresh.timer" not in out


def test_a_bad_refresh_timer_value_is_refused(tmp_path):
    r = run(["--dry-run", "--yes"], tmp_path, {"PORTAL_REFRESH_TIMER": "sometimes"})
    assert r.returncode != 0
    assert "PORTAL_REFRESH_TIMER" in r.stderr


def test_an_install_backfills_stats_as_the_service_user(tmp_path):
    """New figures would otherwise stay empty for every existing graph
    until it happened to be regenerated."""
    out = _install(tmp_path, PORTAL_INSTANCE="staging")
    line = next((x for x in out.splitlines() if "portal-refresh --backfill" in x), "")
    assert "would run: systemd-run" in line, out[-3000:]
    assert "User=portal-staging" in line
    assert "ReadWritePaths=/var/lib/portal-staging" in line
    assert "EnvironmentFile=" in line and "/portal.env" in line
