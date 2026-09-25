#!/usr/bin/env bash
#
# Installer for the Lustre Portal.
#
#   ./install.sh                 # interactive system install (needs root)
#   ./install.sh --dev           # local venv only, nothing system-wide
#   ./install.sh --dry-run       # show every action, change nothing
#   ./install.sh --uninstall     # remove units and site; keeps your data
#   ./install.sh --render-site   # print the nginx site it would write, change nothing
#
# Every answer is remembered in /etc/portal/install.conf, so re-running it
# to upgrade (git pull, then ./install.sh --yes) rebuilds exactly the same
# site. An environment variable still overrides a remembered answer.
#
# The gc CLI and the dashboard come from the bundled vendor/llm_tools by
# default. PORTAL_TOOLS_DIR=/path/to/llm_code_and_review_tools installs
# them from a checkout you manage yourself instead; PORTAL_TOOLS_DIR=bundled
# switches back.
#
# PORTAL_INSTANCE=<name> installs a second, separate portal beside the
# first; PORTAL_REFRESH_TIMER=0 leaves its scheduled refresh off.

set -euo pipefail

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[1;33m'; DIM=$'\033[2m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN=0
DEV=0
ACTION=install
ASSUME_YES=0

# An optional instance name runs a second, fully separate portal on the
# same host -- staging beside production, say. Unset: the names below
# are the plain ones ("portal"). PORTAL_INSTANCE=staging: every name
# gets "-staging" -- directories, service user, systemd units, nginx
# site and its site-extra directory -- so nothing is shared. It picks
# the config directory, so it cannot itself be remembered: pass it on
# every run for a named instance.
INSTANCE="${PORTAL_INSTANCE:-}"
case "$INSTANCE" in
    "") ;;
    *[!a-z0-9-]*|-*) echo "error: PORTAL_INSTANCE must be lowercase letters, digits and dashes" >&2; exit 1 ;;
esac
SUFFIX="${INSTANCE:+-$INSTANCE}"
NAME="portal$SUFFIX"

# Answers from the previous run. Read, never sourced: it is plain
# KEY="value" lines, and nothing in it should be able to run code.
INSTALL_CONF="${PORTAL_CONFIG_DIR:-/etc/$NAME}/install.conf"
saved() {
    [ -r "$INSTALL_CONF" ] || return 0
    sed -n "s/^$1=\"\(.*\)\"\$/\1/p" "$INSTALL_CONF" | tail -1
}
# pick <env value> <remembered key> <default>
pick() {
    if [ -n "$1" ]; then printf '%s' "$1"; return; fi
    local v; v=$(saved "$2")
    printf '%s' "${v:-$3}"
}

# An upgrade used to take every answer from the environment or a
# default, so a plain re-run rebuilt the nginx site with the hostname
# from `hostname -f` -- enough to take a live site offline. Precedence
# is now: explicit environment, then the last run's answer, then default.
APP_DIR=$(pick "${PORTAL_APP_DIR:-}" APP_DIR "/opt/$NAME")
DATA_DIR=$(pick "${PORTAL_DATA_DIR:-}" DATA_DIR "/var/lib/$NAME")
CONFIG_DIR="${PORTAL_CONFIG_DIR:-/etc/$NAME}"
LOG_DIR=$(pick "${PORTAL_LOG_DIR:-}" LOG_DIR /var/log/nginx)
SVC_USER=$(pick "${PORTAL_USER:-}" SVC_USER "$NAME")
PORT=$(pick "${PORTAL_BIND_PORT:-}" PORT 5000)
DASH_PORT=$(pick "${PORTAL_DASH_PORT:-}" DASH_PORT 5056)
SERVER_NAME=$(pick "${PORTAL_SERVER_NAME:-}" SERVER_NAME "")
WANT_DASHBOARD=$(pick "${PORTAL_WITH_DASHBOARD:-}" WITH_DASHBOARD ask)
TOOLS_DIR=$(pick "${PORTAL_TOOLS_DIR:-}" TOOLS_DIR "")
[ "$TOOLS_DIR" = bundled ] && TOOLS_DIR=""
# The scheduled-refresh timer. A staging copy beside production usually
# wants it off, or every graph is regenerated twice against Gerrit.
# PORTAL_REFRESH_TIMER=0 keeps it installed but disabled, and is
# remembered. With no answer yet, a timer an operator has already
# disabled by hand stays disabled: an upgrade used to switch it back on.
REFRESH_TIMER=$(pick "${PORTAL_REFRESH_TIMER:-}" REFRESH_TIMER auto)
if [ "$REFRESH_TIMER" = auto ]; then
    REFRESH_TIMER=1
    if command -v systemctl >/dev/null 2>&1 \
        && [ -e "/etc/systemd/system/$NAME-refresh.timer" ] \
        && [ "$(systemctl is-enabled "$NAME-refresh.timer" 2>/dev/null)" = disabled ]; then
        REFRESH_TIMER=0
    fi
fi
case "$REFRESH_TIMER" in
    1|yes|true) REFRESH_TIMER=1 ;;
    0|no|false) REFRESH_TIMER=0 ;;
    *) echo "error: PORTAL_REFRESH_TIMER must be 1 or 0" >&2; exit 1 ;;
esac
WITH_DASHBOARD=0

# Filled in later, but declared here because render() substitutes all of
# them and `set -u` makes an unset one fatal -- the systemd units render
# before the nginx questions are asked.
VENV=""
TLS_CERT=$(pick "${PORTAL_TLS_CERT:-}" TLS_CERT "")
TLS_KEY=$(pick "${PORTAL_TLS_KEY:-}" TLS_KEY "")
ACME_ROOT=$(pick "${PORTAL_ACME_ROOT:-}" ACME_ROOT "")
NGINX_SITE_DIR=""
NGINX_ENABLE_DIR=""
NGINX_SNIPPET_DIR=""
SITE_EXTRA_DIR=""
ZONE_ID=""

ok()   { printf '%s✓%s %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$NC" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }
step() { printf '\n%s==>%s %s\n' "$GREEN" "$NC" "$*"; }

# Every mutating action goes through run(), so --dry-run is honest
# rather than approximate.
run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '%s  would run:%s %s\n' "$DIM" "$NC" "$*"
    else
        "$@"
    fi
}

write_file() {
    # write_file <path> <mode>; content on stdin
    local path="$1" mode="$2"
    if [ "$DRY_RUN" = 1 ]; then
        printf '%s  would write:%s %s (mode %s)\n' "$DIM" "$NC" "$path" "$mode"
        cat >/dev/null
        return
    fi
    install -d -m 755 "$(dirname "$path")"
    # Create with the final mode before any content lands in it, so a
    # secret is never briefly world-readable.
    ( umask 077; : > "$path" )
    chmod "$mode" "$path"
    cat > "$path"
}

usage() {
    sed -n '/^# Installer/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------- prompts

ask() {
    # ask <variable> <prompt> [default]
    local __var="$1" __prompt="$2" __default="${3:-}" __answer
    if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ]; then
        printf -v "$__var" '%s' "$__default"
        return
    fi
    if [ -n "$__default" ]; then
        read -r -p "$__prompt [$__default]: " __answer || true
    else
        read -r -p "$__prompt: " __answer || true
    fi
    printf -v "$__var" '%s' "${__answer:-$__default}"
}

ask_secret() {
    # Never echoes, never reaches argv or the shell history.
    local __var="$1" __prompt="$2" __answer
    if [ ! -t 0 ]; then printf -v "$__var" '%s' ""; return; fi
    read -r -s -p "$__prompt: " __answer || true
    echo >&2
    printf -v "$__var" '%s' "$__answer"
}

confirm() {
    local prompt="$1" answer
    [ "$ASSUME_YES" = 1 ] && return 0
    [ -t 0 ] || return 1
    read -r -p "$prompt [y/N] " answer || true
    case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- python

find_python() {
    local py version major minor
    for py in python3.13 python3.12 python3.11 python3; do
        command -v "$py" >/dev/null 2>&1 || continue
        version=$("$py" -c 'import sys; print("%d %d" % sys.version_info[:2])')
        major=${version% *}; minor=${version#* }
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            echo "$py"; return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------- nginx

# Distributions disagree about where site files go. Pick the layout that
# actually exists rather than guessing from /etc/os-release.
detect_nginx_layout() {
    NGINX_SITE_DIR=""; NGINX_ENABLE_DIR=""; NGINX_SNIPPET_DIR=""
    command -v nginx >/dev/null 2>&1 || return 1

    if [ -d /etc/nginx/sites-available ]; then          # Debian, Ubuntu
        NGINX_SITE_DIR=/etc/nginx/sites-available
        NGINX_ENABLE_DIR=/etc/nginx/sites-enabled
    elif [ -d /etc/nginx/servers-available ]; then      # some Arch setups
        NGINX_SITE_DIR=/etc/nginx/servers-available
        NGINX_ENABLE_DIR=/etc/nginx/servers-enabled
    elif [ -d /etc/nginx/conf.d ]; then                 # RHEL, Fedora, Arch
        NGINX_SITE_DIR=/etc/nginx/conf.d
        NGINX_ENABLE_DIR=""                             # conf.d is included directly
    else
        return 1
    fi

    # The shared snippets are pulled in by absolute path from the site
    # file, so they live in a directory of their own that nginx never
    # includes on its own. Putting them in conf.d would apply their
    # add_header and proxy_set_header lines to every site on the host.
    NGINX_SNIPPET_DIR=/etc/nginx/portal
    # Per instance: each site's drop-ins proxy to its own portal port and
    # gate on its own sessions, so two instances must never share them.
    SITE_EXTRA_DIR="$NGINX_SNIPPET_DIR/site-extra$SUFFIX"
    return 0
}

render() {
    # render <template> ; substitutions on stdout
    sed \
        -e "s|@APP_DIR@|$APP_DIR|g" \
        -e "s|@DATA_DIR@|$DATA_DIR|g" \
        -e "s|@CONFIG_DIR@|$CONFIG_DIR|g" \
        -e "s|@LOG_DIR@|$LOG_DIR|g" \
        -e "s|@VENV@|$VENV|g" \
        -e "s|@USER@|$SVC_USER|g" \
        -e "s|@GROUP@|$SVC_USER|g" \
        -e "s|@PORT@|$PORT|g" \
        -e "s|@DASH_PORT@|$DASH_PORT|g" \
        -e "s|@SERVER_NAME@|$SERVER_NAME|g" \
        -e "s|@TLS_CERT@|$TLS_CERT|g" \
        -e "s|@TLS_KEY@|$TLS_KEY|g" \
        -e "s|@ACME_ROOT@|$ACME_ROOT|g" \
        -e "s|@SNIPPET_DIR@|$NGINX_SNIPPET_DIR|g" \
        -e "s|@ZONE_ID@|$ZONE_ID|g" \
        -e "s|@SITE_EXTRA@|$SITE_EXTRA_DIR|g" \
        "$1"
}

dashboard_block() {
    [ "$WITH_DASHBOARD" = 1 ] || return 0
    cat <<EOF
    # --- embedded Gerrit dashboard --------------------------------------
    # The portal frames this at /gerrit_dash; the raw app lives here.
    location /gerrit_dash_app/ {
        proxy_pass http://127.0.0.1:${DASH_PORT}/;
        include ${NGINX_SNIPPET_DIR}/portal-proxy.conf;
        proxy_set_header X-Forwarded-Prefix /gerrit_dash_app;
        include ${NGINX_SNIPPET_DIR}/portal-headers.conf;
    }

EOF
}

# Hostname-derived defaults for the site. Separate so --render-site and a
# real install compute them identically.
site_defaults() {
    # nginx shared-memory zone names are global to the instance, so
    # they must differ between two portal sites on one host.
    ZONE_ID=$(printf '%s' "$SERVER_NAME" | tr -c '[:alnum:]' '_' | sed 's/_*$//')
    TLS_CERT="${TLS_CERT:-/etc/letsencrypt/live/$SERVER_NAME/fullchain.pem}"
    TLS_KEY="${TLS_KEY:-/etc/letsencrypt/live/$SERVER_NAME/privkey.pem}"
    ACME_ROOT="${ACME_ROOT:-/var/www/html}"
}

# The complete site file on stdout. The dashboard block is substituted
# separately because it spans several lines, which sed's s/// cannot carry.
render_site() {
    render "$SCRIPT_DIR/deploy/nginx-portal.conf.in" \
        | "$PYTHON" -c '
import sys
sys.stdout.write(sys.stdin.read().replace("@DASHBOARD_BLOCK@", sys.argv[1]))
' "$(dashboard_block)"
}

save_answers() {
    # No secrets live here -- those are in portal.env -- so a plain 0644
    # file is fine, and root can read it back on the next run.
    [ "$DRY_RUN" = 1 ] && { echo "  would write: $INSTALL_CONF"; return; }
    install -d -m 755 "$CONFIG_DIR"
    {
        echo "# Written by install.sh on $(date -u '+%Y-%m-%d %H:%M UTC')."
        echo "# The answers a re-run reuses. An environment variable overrides any"
        echo "# of them for one run; edit here to change one for good."
        for k in APP_DIR DATA_DIR LOG_DIR SVC_USER PORT DASH_PORT SERVER_NAME \
                 ACME_ROOT WITH_DASHBOARD TOOLS_DIR REFRESH_TIMER; do
            printf '%s="%s"\n' "$k" "${!k}"
        done
        # Only a customised certificate path is remembered. The default is
        # derived from the hostname, and remembering it would keep loading
        # the old certificate after the hostname changed.
        [ "$TLS_CERT" != "/etc/letsencrypt/live/$SERVER_NAME/fullchain.pem" ] && printf 'TLS_CERT="%s"\n' "$TLS_CERT"
        [ "$TLS_KEY"  != "/etc/letsencrypt/live/$SERVER_NAME/privkey.pem"   ] && printf 'TLS_KEY="%s"\n' "$TLS_KEY"
        true
    } > "$INSTALL_CONF"
    chmod 644 "$INSTALL_CONF"
}

do_render_site() {
    PYTHON=$(find_python) || die "Python 3.11 or newer is required."
    NGINX_SNIPPET_DIR=/etc/nginx/portal
    SITE_EXTRA_DIR="$NGINX_SNIPPET_DIR/site-extra$SUFFIX"
    [ -n "$SERVER_NAME" ] || die "No hostname: set PORTAL_SERVER_NAME, or run a full install first."
    case "$WANT_DASHBOARD" in 1|yes|true) WITH_DASHBOARD=1 ;; esac
    site_defaults
    render_site
}

# ---------------------------------------------------------------- install

do_install() {
    step "Checking prerequisites"

    local PYTHON
    PYTHON=$(find_python) || die "Python 3.11 or newer is required."
    ok "Python: $PYTHON ($("$PYTHON" -V 2>&1 | cut -d' ' -f2))"

    if [ "$DEV" = 0 ] && [ "$(id -u)" != 0 ] && [ "$DRY_RUN" = 0 ]; then
        die "A system install needs root. Use sudo, or --dev for a local venv."
    fi

    # Two instances on one host must not share a port. Refuse before
    # anything is written, unless it is this instance holding it.
    if [ "$DEV" = 0 ] && [ "$DRY_RUN" = 0 ] && command -v ss >/dev/null 2>&1 \
        && ss -ltnH "sport = :$PORT" | grep -q . \
        && ! systemctl is-active --quiet "$NAME.service"; then
        die "Port $PORT is already in use by something other than $NAME.service. Pick another with PORTAL_BIND_PORT."
    fi

    if [ -n "$TOOLS_DIR" ]; then
        [ -f "$TOOLS_DIR/gerrit_cli/pyproject.toml" ] \
            || die "PORTAL_TOOLS_DIR=$TOOLS_DIR is not an llm_code_and_review_tools checkout (no gerrit_cli/)."
        ok "gc CLI from your checkout: $TOOLS_DIR"
    elif [ ! -e "$SCRIPT_DIR/vendor/llm_tools/gerrit_cli/pyproject.toml" ]; then
        step "Fetching the gc CLI"
        # Not --recursive on purpose: that repo has a nested submodule
        # over SSH, which fails for anyone without the right keys, and
        # nothing here needs it.
        run git -C "$SCRIPT_DIR" submodule update --init vendor/llm_tools \
            || die "Could not fetch vendor/llm_tools. Clone with --recurse-submodules=no and retry."
        ok "gc CLI source present"
    else
        ok "gc CLI source present"
    fi

    # ---- paths
    if [ "$DEV" = 1 ]; then
        APP_DIR="$SCRIPT_DIR"
        DATA_DIR="${PORTAL_DATA_DIR:-$SCRIPT_DIR/data}"
        CONFIG_DIR="$SCRIPT_DIR"
        VENV="$SCRIPT_DIR/.venv"
    else
        step "Where should this live?"
        ask APP_DIR   "Application directory" "$APP_DIR"
        ask DATA_DIR  "Data directory (graphs, accounts)" "$DATA_DIR"
        ask CONFIG_DIR "Configuration directory" "$CONFIG_DIR"
        ask SVC_USER  "Service user (created if missing)" "$SVC_USER"
        VENV="$APP_DIR/.venv"
    fi

    # ---- service user
    if [ "$DEV" = 0 ] && ! id -u "$SVC_USER" >/dev/null 2>&1; then
        step "Creating service user $SVC_USER"
        run useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER" \
            || run useradd --system --home-dir "$DATA_DIR" --shell /sbin/nologin "$SVC_USER"
        ok "created $SVC_USER"
    fi

    # ---- code
    if [ "$DEV" = 0 ] && [ "$APP_DIR" != "$SCRIPT_DIR" ]; then
        step "Copying the application to $APP_DIR"
        run install -d -m 755 "$APP_DIR"
        run cp -R "$SCRIPT_DIR/portal" "$SCRIPT_DIR/deploy" "$SCRIPT_DIR/vendor" \
                  "$SCRIPT_DIR/pyproject.toml" "$SCRIPT_DIR/README.md" \
                  "$SCRIPT_DIR/LICENSE" "$APP_DIR/"
        ok "copied"
    fi

    step "Creating the virtual environment"
    if [ ! -x "$VENV/bin/python" ]; then
        run "$PYTHON" -m venv "$VENV"
    fi
    run "$VENV/bin/pip" install -q --upgrade pip
    ok "$VENV"

    step "Installing packages"
    # gerrit-cli first: gerrit-dashboard needs it, and deliberately does
    # not declare it as a dependency because an unrelated project owns
    # that name on PyPI.
    local pkg
    if [ -n "$TOOLS_DIR" ]; then
        # A checkout outside the app is installed as a COPY, not an
        # editable link. The service runs unprivileged under ProtectHome,
        # so at run time it usually cannot read such a checkout at all --
        # one under /root, say. The price: after pulling that checkout,
        # re-run this script to pick the new version up.
        for pkg in llm_tool_common gerrit_cli gerrit_dashboard; do
            run "$VENV/bin/pip" install -q "$TOOLS_DIR/$pkg"       # dependencies
            run "$VENV/bin/pip" install -q --no-deps --force-reinstall "$TOOLS_DIR/$pkg"
        done
        ok "gerrit-cli and gerrit-dashboard installed from $TOOLS_DIR ($(git -C "$TOOLS_DIR" log -1 --format='%h %ad' --date=short 2>/dev/null || echo 'not a git checkout'))"
    else
        for pkg in llm_tool_common gerrit_cli gerrit_dashboard; do
            run "$VENV/bin/pip" install -q -e "$APP_DIR/vendor/llm_tools/$pkg"
        done
        ok "gerrit-cli and gerrit-dashboard installed from the bundled vendor/llm_tools"
    fi
    run "$VENV/bin/pip" install -q -e "$APP_DIR"
    ok "portal installed"

    step "Creating $DATA_DIR"
    run install -d -m 750 "$DATA_DIR"
    [ "$DEV" = 0 ] && run chown -R "$SVC_USER:$SVC_USER" "$DATA_DIR"
    ok "$DATA_DIR"

    # ---- configuration
    local ENV_FILE="$CONFIG_DIR/portal.env"
    [ "$DEV" = 1 ] && ENV_FILE="$CONFIG_DIR/.env"

    if [ -f "$ENV_FILE" ]; then
        ok "keeping existing $ENV_FILE"
    else
        step "Configuration"
        local SECRET SITE_NAME GERRIT_URL GERRIT_USER GERRIT_PASS PUBLIC_PROJECT
        # $PYTHON, not the venv: --dry-run never creates the venv.
        SECRET=$("$PYTHON" -c 'import secrets; print(secrets.token_hex(32))')
        ask SITE_NAME "Site name (shown in the header)" "${PORTAL_SITE_NAME:-Lustre Tools}"
        ask GERRIT_URL "Gerrit URL" "${GERRIT_URL:-https://review.whamcloud.com}"
        ask PUBLIC_PROJECT "Project everyone may graph" "${PORTAL_PUBLIC_PROJECT:-fs/lustre-release}"
        echo
        echo "Gerrit credentials are optional. Without them the portal queries"
        echo "Gerrit anonymously, which is enough for public projects only."
        ask GERRIT_USER "Gerrit HTTP username (blank for anonymous)" "${GERRIT_USER:-}"
        if [ -n "$GERRIT_USER" ] && [ -z "${GERRIT_PASS:-}" ]; then
            ask_secret GERRIT_PASS "Gerrit HTTP password"
        fi

        write_file "$ENV_FILE" 600 <<EOF
# Written by install.sh on $(date -u '+%Y-%m-%d %H:%M UTC'). Secrets live
# here; keep the mode at 600 and out of version control.
# Values are double-quoted: systemd, python-dotenv and a shell all read
# that the same way, and a site name with a space or a bracket in it is
# then not a syntax error for anyone.
PORTAL_SECRET_KEY="$SECRET"
PORTAL_SITE_NAME="$SITE_NAME"
PORTAL_DATA_DIR="$DATA_DIR"
PORTAL_BIND_HOST="127.0.0.1"
PORTAL_BIND_PORT="$PORT"
PORTAL_PUBLIC_PROJECT="$PUBLIC_PROJECT"
GERRIT_URL="$GERRIT_URL"
GERRIT_USER="$GERRIT_USER"
GERRIT_PASS="${GERRIT_PASS:-}"
EOF
        if [ "$DEV" = 0 ]; then
            # Readable by the service, writable only by root.
            run chown "root:$SVC_USER" "$ENV_FILE"
            run chmod 640 "$ENV_FILE"
            ok "wrote $ENV_FILE (mode 640, root:$SVC_USER)"
        else
            ok "wrote $ENV_FILE (mode 600)"
        fi
        echo "    See .env.example for everything else you can set."
    fi

    # ---- first account
    if [ ! -f "$DATA_DIR/users.json" ] && [ "$DRY_RUN" = 0 ]; then
        step "First account"
        local ADMIN_USER ADMIN_PASS ADMIN_PASS2
        ask ADMIN_USER "Admin username" "${PORTAL_ADMIN_USER:-admin}"
        if [ -t 0 ] && [ -n "$ADMIN_USER" ]; then
            ask_secret ADMIN_PASS "Password for $ADMIN_USER"
            ask_secret ADMIN_PASS2 "Repeat"
            if [ -z "$ADMIN_PASS" ] || [ "$ADMIN_PASS" != "$ADMIN_PASS2" ]; then
                warn "Passwords empty or did not match; skipping. Create one later with:"
                echo "    $VENV/bin/portal-users set-password --user $ADMIN_USER"
            else
                # Via stdin, so it never appears in argv or the history.
                printf '%s\n' "$ADMIN_PASS" | \
                    PORTAL_USERS_FILE="$DATA_DIR/users.json" \
                    "$VENV/bin/portal-users" set-password --user "$ADMIN_USER" --password-stdin >/dev/null
                PORTAL_USERS_FILE="$DATA_DIR/users.json" \
                    "$VENV/bin/portal-users" add-role --user "$ADMIN_USER" --role admin >/dev/null
                [ "$DEV" = 0 ] && chown "$SVC_USER:$SVC_USER" "$DATA_DIR/users.json"
                ok "created $ADMIN_USER with the admin role"
            fi
        fi
    fi

    if [ "$DEV" = 1 ]; then
        step "Done"
        if [ ! -f "$DATA_DIR/users.json" ]; then
            echo "No account yet. Create one with:"
            echo "    $VENV/bin/portal-users set-password --user admin"
            echo "    $VENV/bin/portal-users add-role --user admin --role admin"
            echo
        fi
        echo "Run it with:"
        echo "    $VENV/bin/portal"
        echo "Then open http://127.0.0.1:$PORT/"
        return
    fi

    # ---- optional dashboard
    # PORTAL_WITH_DASHBOARD=0 declines without asking. Needed because
    # --yes otherwise always accepts, and a host that already runs its
    # own dashboard would get a second one competing for the port.
    local want_dash
    case "$WANT_DASHBOARD" in
        0|no|false)  want_dash=1 ;;
        1|yes|true)  want_dash=0 ;;
        *)           confirm "Also run the public Gerrit dashboard and embed it?" && want_dash=0 || want_dash=1 ;;
    esac
    if [ "$want_dash" = 0 ]; then
        WITH_DASHBOARD=1
        run install -d -m 750 "$DATA_DIR/dashboard-public"
        run chown "$SVC_USER:$SVC_USER" "$DATA_DIR/dashboard-public"
        if [ "$DRY_RUN" = 0 ] && ! grep -q PORTAL_DASHBOARD_PREFIX "$ENV_FILE"; then
            printf 'PORTAL_DASHBOARD_PREFIX="/gerrit_dash"\nPORTAL_DASHBOARD_APP_PATH="/gerrit_dash_app/"\n' >> "$ENV_FILE"
        fi
    fi

    # ---- systemd
    step "Installing systemd units"
    render "$SCRIPT_DIR/deploy/portal.service.in" | write_file "/etc/systemd/system/$NAME.service" 644
    render "$SCRIPT_DIR/deploy/portal-refresh.service.in" | write_file "/etc/systemd/system/$NAME-refresh.service" 644
    write_file "/etc/systemd/system/$NAME-refresh.timer" 644 < "$SCRIPT_DIR/deploy/portal-refresh.timer"
    if [ "$WITH_DASHBOARD" = 1 ]; then
        render "$SCRIPT_DIR/deploy/gerrit-dashboard.service.in" | write_file "/etc/systemd/system/$NAME-dashboard.service" 644
    fi
    run systemctl daemon-reload
    ok "units installed"

    # ---- nginx
    if detect_nginx_layout; then
        step "Configuring nginx ($NGINX_SITE_DIR)"
        ask SERVER_NAME "Public hostname" "${SERVER_NAME:-$(hostname -f 2>/dev/null || hostname)}"
        site_defaults
        ask TLS_CERT "TLS certificate" "$TLS_CERT"
        ask TLS_KEY  "TLS private key" "$TLS_KEY"
        ask ACME_ROOT "ACME webroot (for certificate renewal)" "$ACME_ROOT"

        run install -d -m 755 "$SITE_EXTRA_DIR"
        write_file "$NGINX_SNIPPET_DIR/portal-proxy.conf" 644   < "$SCRIPT_DIR/deploy/portal-proxy.conf"
        write_file "$NGINX_SNIPPET_DIR/portal-headers.conf" 644 < "$SCRIPT_DIR/deploy/portal-headers.conf"

        local site="$NGINX_SITE_DIR/$NAME.conf"
        render_site | write_file "$site" 644

        # Behind this nginx, the app must trust exactly one proxy hop, or
        # every client is 127.0.0.1 and per-IP limiting means nothing.
        if [ "$DRY_RUN" = 0 ] && ! grep -q "^PORTAL_PROXY_HOPS=" "$CONFIG_DIR/portal.env" 2>/dev/null; then
            printf 'PORTAL_PROXY_HOPS="1"\n' >> "$CONFIG_DIR/portal.env"
        fi

        if [ -n "$NGINX_ENABLE_DIR" ]; then
            run install -d -m 755 "$NGINX_ENABLE_DIR"
            run ln -sf "$site" "$NGINX_ENABLE_DIR/$NAME.conf"
        fi

        if [ "$DRY_RUN" = 0 ]; then
            if nginx -t 2>/dev/null; then
                run systemctl reload nginx
                ok "nginx configured and reloaded"
            else
                warn "nginx -t failed; the site file is written but NOT active."
                echo "    Check it, then: nginx -t && systemctl reload nginx"
                nginx -t || true
            fi
        fi
    else
        warn "nginx not found, or no recognised site directory."
        echo "    The app is on 127.0.0.1:$PORT; put your own proxy in front."
        echo "    A starting point: $SCRIPT_DIR/deploy/nginx-portal.conf.in"
    fi

    # ---- start
    step "Starting services"
    # restart, not "enable --now". enable --now starts a stopped unit and
    # does nothing to a running one, so re-running this script to upgrade
    # -- which is what the README says to do -- installed the new code and
    # left the old process serving it. It then reported the stale process
    # as running.
    local started
    started=$(date +%s)
    run systemctl enable "$NAME.service"
    run systemctl restart "$NAME.service"
    if [ "$REFRESH_TIMER" = 1 ]; then
        run systemctl enable "$NAME-refresh.timer"
        run systemctl restart "$NAME-refresh.timer"
    else
        run systemctl disable --now "$NAME-refresh.timer" 2>/dev/null || true
        ok "scheduled refresh left off (PORTAL_REFRESH_TIMER=1 turns it on)"
    fi
    if [ "$WITH_DASHBOARD" = 1 ]; then
        run systemctl enable "$NAME-dashboard.service"
        run systemctl restart "$NAME-dashboard.service"
    fi

    if [ "$DRY_RUN" = 0 ]; then
        sleep 2
        if ! systemctl is-active --quiet "$NAME.service"; then
            die "$NAME.service failed to start. journalctl -u $NAME.service -n 50"
        fi
        # Prove the process is the one just started, not a survivor.
        local since
        since=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$NAME.service")
        local up_for=$(( $(cut -d. -f1 /proc/uptime) - since / 1000000 ))
        if [ "$up_for" -gt $(( $(date +%s) - started + 5 )) ]; then
            die "$NAME.service is running, but it did not restart -- it is still serving the old code"
        fi
        ok "$NAME.service is running the code just installed"
    fi

    # A new version may show figures older graphs do not have in the
    # index yet. Read them from the graph files already on disk --
    # nothing is regenerated and Gerrit is not asked. Run as the service
    # user, with its configuration and its sandbox, like the timer does.
    step "Reading stats from the graphs already generated"
    if run systemd-run --quiet --wait --pipe --collect \
            -p User="$SVC_USER" -p Group="$SVC_USER" \
            -p EnvironmentFile="$ENV_FILE" -p WorkingDirectory="$APP_DIR" \
            -p ProtectSystem=strict -p ReadWritePaths="$DATA_DIR" \
            "$VENV/bin/portal-refresh" --backfill; then
        ok "graph stats up to date"
    else
        warn "could not read stats from the existing graphs; they fill in on each graph's next regeneration."
        echo "    To retry: re-run this installer"
    fi

    save_answers

    step "Done"
    echo "  Site:     https://${SERVER_NAME:-localhost}/"
    echo "  Config:   $ENV_FILE"
    echo "  Data:     $DATA_DIR"
    echo "  Accounts: $VENV/bin/portal-users list"
    echo "  Logs:     journalctl -u $NAME.service -f"
}

do_uninstall() {
    [ "$(id -u)" = 0 ] || die "Uninstalling needs root."
    step "Stopping services"
    for unit in "$NAME.service" "$NAME-refresh.timer" "$NAME-refresh.service" "$NAME-dashboard.service"; do
        run systemctl disable --now "$unit" 2>/dev/null || true
        run rm -f "/etc/systemd/system/$unit"
    done
    run systemctl daemon-reload

    step "Removing the nginx site"
    if detect_nginx_layout; then
        run rm -f "$NGINX_SITE_DIR/$NAME.conf"
        [ -n "$NGINX_ENABLE_DIR" ] && run rm -f "$NGINX_ENABLE_DIR/$NAME.conf"
        run rm -rf "$NGINX_SNIPPET_DIR"
        if nginx -t >/dev/null 2>&1; then run systemctl reload nginx; fi
    fi

    ok "Units and site removed."
    echo
    echo "Left in place on purpose -- delete by hand if you mean it:"
    echo "  $DATA_DIR     (graphs, index, accounts)"
    echo "  $CONFIG_DIR   (portal.env, contains your secret key)"
    echo "  $APP_DIR      (code and venv)"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)   usage; exit 0 ;;
        --dry-run)   DRY_RUN=1 ;;
        --dev)       DEV=1 ;;
        --yes|-y)    ASSUME_YES=1 ;;
        --uninstall) ACTION=uninstall ;;
        --render-site) ACTION=render ;;
        *)           die "Unknown option: $1  (try --help)" ;;
    esac
    shift
done

case "$ACTION" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
    render)    do_render_site ;;
esac
