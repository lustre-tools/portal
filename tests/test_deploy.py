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
    "LOG_DIR": "/var/log/nginx",
    "VENV": "/opt/portal/.venv",
    "USER": "portal",
    "GROUP": "portal",
    "PORT": "5000",
    "DASH_PORT": "5056",
    "SERVER_NAME": "portal.example.org",
    "TLS_CERT": "/etc/letsencrypt/live/portal.example.org/fullchain.pem",
    "TLS_KEY": "/etc/letsencrypt/live/portal.example.org/privkey.pem",
    "ACME_ROOT": "/var/www/html",
    "SNIPPET_DIR": "/etc/nginx/portal",
    "ZONE_ID": "portal_example_org",
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
    """The zone is declared in the site file itself. A separate snippet
    would need an http-level include directory, and not every layout has
    one -- Arch's servers-enabled setup does not -- so it silently ended
    up somewhere nginx never read."""
    site = render("nginx-portal.conf.in")
    assert "limit_req_zone $binary_remote_addr zone=portal_login" in site
    assert "limit_req zone=portal_login" in site
    assert site.index("limit_req_zone") < site.index("limit_req zone="), (
        "the zone must be declared before it is used"
    )


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


def test_nginx_shared_zones_are_unique_per_site():
    """Zone names are global to the nginx instance, not to a site file.

    Two portal installs on one host that both declare zone=portal_login
    is a hard [emerg] "already bound to key" and nginx refuses to start.
    Staging plus production on one box is the obvious way to hit it, and
    it did: `nginx -t` failed the moment a second site was enabled. So
    every shared zone name carries the @ZONE_ID@ the installer derives
    from the server name.
    """
    with open(os.path.join(DEPLOY, "nginx-portal.conf.in")) as f:
        raw = f.read()
    zones = re.findall(r"zone=([A-Za-z_@]+):", raw) + re.findall(r"shared:([A-Za-z_@]+):", raw)
    assert zones, "no shared zones found -- did the template change?"
    for z in zones:
        assert "@ZONE_ID@" in z, (
            f"zone {z!r} has no @ZONE_ID@; a second portal site on the same "
            f"host would fail nginx -t with 'already bound to key'"
        )
    # And every use must reference the same parameterised name.
    for used in re.findall(r"limit_req zone=([A-Za-z_@]+) ", raw):
        assert "@ZONE_ID@" in used, used


def test_installer_derives_a_zone_id_from_the_server_name():
    with open(os.path.join(ROOT, "install.sh")) as f:
        text = f.read()
    assert "ZONE_ID=$(printf" in text
    assert "s|@ZONE_ID@|$ZONE_ID|g" in text


def test_nginx_headers_restate_everything_the_global_block_sets():
    """add_header discards every inherited header, so a partial list here
    silently removes the rest. The site this replaced set seven; dropping
    X-Robots-Tag alone would have made every generated graph indexable.
    """
    with open(os.path.join(DEPLOY, "portal-headers.conf")) as f:
        snippet = f.read()
    for header in (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "X-Frame-Options",
        "X-Robots-Tag",
        "X-Download-Options",
        "X-Permitted-Cross-Domain-Policies",
    ):
        assert header in snippet, f"{header} missing -- it would be silently dropped"


def test_nginx_shared_zones_have_portal_specific_names():
    """Shared-memory zone names are global to the nginx instance. A
    generic name such as "SSL" collides with any other site on the host
    that used the same name at a different size, and nginx -t fails."""
    site = render("nginx-portal.conf.in")
    for zone in re.findall(r"shared:([A-Za-z_]+):", site):
        assert zone.startswith("portal"), f"zone {zone!r} is not portal-specific"
    for zone in re.findall(r"zone=([A-Za-z_]+):", site):
        assert zone.startswith("portal"), f"zone {zone!r} is not portal-specific"


def test_nginx_has_a_hook_for_site_specific_locations():
    """A deployment usually serves something the portal does not -- a
    static directory, another gated service. Without a hook the only
    way to add one is to edit the generated site file, which the next
    install overwrites.

    What matters is that the include sits in the HTTPS server block, not
    the plain-HTTP redirect one. File order does not affect which
    location nginx picks -- it matches the longest prefix regardless.
    """
    site = render("nginx-portal.conf.in")
    assert "site-extra/*.conf" in site

    blocks = site.split("server {")
    https = next(b for b in blocks if "listen 443" in b)
    assert "site-extra/*.conf" in https, (
        "the hook must be in the HTTPS server block; in the redirect "
        "block every request is answered by the 301 before it is reached"
    )


def test_installer_creates_the_site_extra_directory():
    """An include of a glob that matches nothing is fine in nginx, but
    the directory itself has to exist."""
    with open(os.path.join(ROOT, "install.sh")) as f:
        text = f.read()
    assert 'install -d -m 755 "$NGINX_SNIPPET_DIR/site-extra"' in text


def test_installer_can_decline_the_dashboard_non_interactively():
    """--yes answers every prompt yes. A host that already runs its own
    dashboard must be able to say no, or the installer starts a second
    one competing for the same port."""
    with open(os.path.join(ROOT, "install.sh")) as f:
        text = f.read()
    assert "PORTAL_WITH_DASHBOARD" in text
    decline = text[text.index('case "${PORTAL_WITH_DASHBOARD') :]
    assert "0|no|false" in decline[:400]


def test_nginx_hides_upstream_copies_of_the_headers_it_sets():
    """The app sets its own security headers and nginx adds them again.
    Without proxy_hide_header each reached the browser twice -- and a
    doubled X-Frame-Options can be treated as invalid and ignored."""
    with open(os.path.join(DEPLOY, "portal-headers.conf")) as f:
        snippet = f.read()
    added = set(re.findall(r"^add_header\s+(\S+)", snippet, re.M))
    hidden = set(re.findall(r"^proxy_hide_header\s+(\S+);", snippet, re.M))
    assert added, "no headers found"
    assert added <= hidden, f"added but not hidden upstream: {sorted(added - hidden)}"


def test_nginx_logs_go_where_logrotate_looks():
    """Every distribution rotates /var/log/nginx/*log. A log written
    anywhere else grows without bound."""
    site = render("nginx-portal.conf.in")
    for path in re.findall(r"(?:access|error)_log\s+(\S+);", site):
        assert path.startswith("/var/log/nginx/"), path
        assert "@ZONE_ID@" not in path and "portal_example_org" in path, (
            "logs must be named per site so two installs do not share one"
        )


def test_authcheck_is_not_reachable_from_a_browser():
    """It exists for nginx subrequests. The gate it replaced was marked
    internal, so a direct request was a 404; keep that."""
    site = render("nginx-portal.conf.in")
    block = site[site.index("location = /_authcheck") :]
    block = block[: block.index("}")]
    assert "internal;" in block


def test_installer_sets_one_proxy_hop_behind_nginx():
    with open(os.path.join(ROOT, "install.sh")) as f:
        text = f.read()
    assert 'PORTAL_PROXY_HOPS="1"' in text


def test_readme_does_not_document_a_role_in_auth_request():
    """auth_request does not pass a query string. Written that way the
    gate returns 500 for every visitor -- measured, not assumed. The
    README once showed exactly that."""
    with open(os.path.join(ROOT, "README.md")) as f:
        readme = f.read()
    code = "\n".join(re.findall(r"```nginx\n(.*?)```", readme, re.S))
    assert not re.search(r"^\s*auth_request\s+\S*\?", code, re.M), (
        "a role in auth_request itself does not work"
    )
