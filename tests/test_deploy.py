"""The deploy templates render to something complete and consistent.

These are cheap checks, but they catch the class of bug that only shows
up on someone else's machine: a placeholder nobody substituted, a unit
that writes outside its ReadWritePaths, or a config that quietly relies
on a global nginx block the operator does not have.
"""

import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")

PLACEHOLDER_RE = re.compile(r"@[A-Z_]+@")

SUBSTITUTIONS = {
    "APP_DIR": "/opt/portal",
    "DATA_DIR": "/var/lib/portal",
    "CONFIG_DIR": "/etc/portal",
    "LOG_DIR": "/var/log",
    "VENV": "/opt/portal/.venv",
    "USER": "portal",
    "GROUP": "portal",
    "PORT": "5000",
    "DASH_PORT": "5056",
    "SERVER_NAME": "portal.example.org",
    "TLS_CERT": "/etc/letsencrypt/live/portal.example.org/fullchain.pem",
    "TLS_KEY": "/etc/letsencrypt/live/portal.example.org/privkey.pem",
    "ACME_ROOT": "/var/www/html",
    "SNIPPET_DIR": "/etc/nginx/conf.d",
    "DASHBOARD_BLOCK": "",
}


def render(name):
    with open(os.path.join(DEPLOY, name)) as f:
        text = f.read()
    for key, value in SUBSTITUTIONS.items():
        text = text.replace(f"@{key}@", value)
    return text


TEMPLATES = [f for f in sorted(os.listdir(DEPLOY)) if f.endswith(".in")]


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_placeholder_is_substituted(name):
    """A template must not carry a placeholder the installer never fills."""
    leftover = PLACEHOLDER_RE.findall(render(name))
    assert not leftover, f"{name} has unsubstituted placeholders: {leftover}"


def test_templates_exist():
    assert TEMPLATES, "no .in templates found in deploy/"


@pytest.mark.parametrize("name", [t for t in TEMPLATES if t.endswith(".service.in")])
def test_units_are_confined(name):
    """Every unit runs unprivileged and read-only apart from its data."""
    text = render(name)
    assert "User=portal" in text, f"{name} must not run as root"
    assert "ProtectSystem=strict" in text
    assert "NoNewPrivileges=true" in text
    assert "ReadWritePaths=/var/lib/portal" in text, (
        f"{name} must declare exactly the directory it needs to write"
    )


def test_units_load_the_env_file():
    for name in TEMPLATES:
        if not name.endswith(".service.in"):
            continue
        assert "EnvironmentFile=/etc/portal/portal.env" in render(name), name


def test_public_dashboard_is_scoped_to_the_public_project():
    """GD_COMMUNITY is the only thing making an unauthenticated dashboard
    safe: it scopes the Gerrit queries, not just the rendering."""
    text = render("gerrit-dashboard.service.in")
    assert "Environment=GD_COMMUNITY=1" in text
    assert "GD_DATA_DIR=/var/lib/portal/dashboard-public" in text


def test_dashboard_does_not_share_a_data_dir_with_anything():
    """Two dashboard instances sharing a data dir would mix an
    internally-scoped snapshot into the public one."""
    text = render("gerrit-dashboard.service.in")
    data_dirs = re.findall(r"GD_DATA_DIR=(\S+)", text)
    assert data_dirs == ["/var/lib/portal/dashboard-public"]


# ---------- nginx ----------


def test_nginx_site_sets_its_own_security_headers():
    """The site must not depend on headers from a global http{} block.

    nginx's add_header does not merge: a single add_header in a location
    drops every inherited one. A deployment without a global block would
    otherwise ship with no headers at all and nobody would notice.
    """
    headers = render("nginx-portal.conf.in")
    with open(os.path.join(DEPLOY, "portal-headers.conf")) as f:
        snippet = f.read()

    for header in (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "X-Frame-Options",
    ):
        assert header in snippet, f"{header} missing from portal-headers.conf"

    assert "portal-headers.conf" in headers, "the site must include the header snippet"


def test_nginx_headers_use_always():
    """Without `always` the headers are skipped on error responses."""
    with open(os.path.join(DEPLOY, "portal-headers.conf")) as f:
        for line in f:
            if line.startswith("add_header"):
                assert line.rstrip().endswith("always;"), line


def test_nginx_forwards_the_host_header():
    """The embedded dashboard compares Origin against X-Forwarded-Host
    for CSRF; without it every POST is rejected."""
    with open(os.path.join(DEPLOY, "portal-proxy.conf")) as f:
        proxy = f.read()
    assert "X-Forwarded-Host" in proxy
    assert "X-Forwarded-For" in proxy


def test_nginx_rate_limits_the_login_endpoint():
    site = render("nginx-portal.conf.in")
    assert "limit_req zone=portal_login" in site
    with open(os.path.join(DEPLOY, "portal-ratelimit.conf")) as f:
        assert "limit_req_zone" in f.read()


def test_nginx_websocket_location_upgrades():
    site = render("nginx-portal.conf.in")
    block = site[site.index("location /socket.io/") :]
    block = block[: block.index("\n    location") if "\n    location" in block else len(block)]
    assert "proxy_set_header Upgrade $http_upgrade;" in block
    assert 'Connection "upgrade"' in block
    # A graph run streams for minutes; the default 60s would cut it off.
    assert "proxy_read_timeout" in block


def test_nginx_leaves_acme_on_plain_http():
    """Otherwise the first certificate renewal after install fails."""
    site = render("nginx-portal.conf.in")
    assert "/.well-known/acme-challenge/" in site
    redirect_at = site.index("return 301 https://")
    acme_at = site.index("/.well-known/acme-challenge/")
    assert acme_at < redirect_at, "ACME must be matched before the redirect"


# ---------- the installer itself ----------


def test_install_script_is_syntactically_valid():
    path = os.path.join(ROOT, "install.sh")
    subprocess.run(["bash", "-n", path], check=True)


def test_install_script_never_passes_a_password_as_an_argument():
    """argv is visible in ps and lands in shell history."""
    with open(os.path.join(ROOT, "install.sh")) as f:
        text = f.read()
    assert "--password " not in text.replace("--password-stdin", "")
    assert "--password-stdin" in text


def test_env_example_has_no_real_values():
    """The example must never ship a usable secret."""
    with open(os.path.join(ROOT, ".env.example")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            assert value == "", f"{key} must be blank in .env.example, got a value"
