"""The installer, run for real in its non-destructive modes.

--render-site prints the nginx site and changes nothing; --dry-run prints
every action and takes none. Both are enough to check the two things that
matter most on a live host: that a re-run rebuilds the SAME site from the
remembered answers, and where the gc CLI is installed from.
"""

import os
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
