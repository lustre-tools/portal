# Security

## Reporting a vulnerability

Please report privately, not in a public issue.

Use GitHub's [private vulnerability reporting][gh] on this repository
(Security → Report a vulnerability). If that is unavailable to you, open
an issue saying only that you have a security report and asking for a
contact — no details — and a maintainer will get in touch.

[gh]: https://github.com/lustre-tools/portal/security/advisories/new

Useful things to include: what an attacker can do, the steps to
reproduce it, and which version or commit you tested. A proof of concept
is welcome but not required.

This is a volunteer project, so there is no guaranteed response time. We
will confirm receipt, tell you whether we agree it is a problem, and
credit you in the fix unless you would rather we did not.

## What is in scope

The portal's own code: authentication and roles, the public/internal
split, the graph tool, the deploy templates, and the installer.

Out of scope, because they are someone else's to fix:

- Gerrit, Jira, and the `gc` CLI in
  [llm_code_and_review_tools](https://github.com/lustre-tools/llm_code_and_review_tools)
- a deployment's own nginx, TLS or operating system configuration
- anything that requires an account you were given legitimately doing
  what that account's roles allow

## What the design assumes

Knowing these may save you time, and if any of them is wrong, that is
itself worth reporting.

**The public/internal split is the main boundary.** A graph belongs to a
project; anything other than `PORTAL_PUBLIC_PROJECT` is internal and
requires the `internal` role. Internal entries are filtered out of the
list, out of label autocomplete, and out of re-run data, and a direct
URL returns a 404 identical to the one for a graph that does not exist.
Anything that lets an unprivileged session learn that an internal graph
exists is a bug worth reporting, including by timing or error-message
differences.

**Generated graph HTML is world-readable.** A graph of the public
project is never given `--cross-project`, even for a privileged user,
because the embedded JSON would then carry identifiers from other
projects into a file anyone can fetch.

**The session cookie is the whole session.** There is no server-side
session store, so an individual session cannot be revoked. Deleting an
account or removing a role *does* take effect immediately — both are
looked up on every request. Rotating `PORTAL_SECRET_KEY` invalidates
every session at once.

**A WebSocket carries cookies only at handshake.** The identity on an
open socket is therefore fixed for the connection's lifetime. Whether
that account still exists, and what roles it holds, is re-checked on
every event. The client opens one socket per run and closes it on
completion.

**Tool output is filtered, not trusted.** `gc` output is scrubbed of
server filesystem paths before it reaches the browser. If you can get a
path or a traceback through, that is a bug.

**Gerrit credentials define exposure.** The portal queries Gerrit with
one service account. If that account can read a project, the portal can
too, and only the role check stands between that and a user. Give it the
narrowest access your deployment needs.

## Known limitations

These are understood and accepted rather than overlooked:

- No account lockout after repeated failures. Rate limiting is done by
  nginx, per IP, on the login endpoint only.
- No audit log of who generated, renamed or deleted what.
- No web UI for account management; it is a command-line tool that needs
  shell access on the server.
- A scheduled refresh runs with internal access, since only an admin can
  create one. A graph scheduled while a project was public keeps
  refreshing if that project later becomes internal.
