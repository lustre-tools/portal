# Notes for AI assistants

Orientation for an agent working in this repository. Human contributors
want [CONTRIBUTING.md](CONTRIBUTING.md); the threat model is in
[SECURITY.md](SECURITY.md).

## What this is

A Flask + Flask-SocketIO app that puts Lustre development tools behind a
browser. It generates Gerrit patch-series graphs by shelling out to the
`gc` CLI and streaming its output over a WebSocket.

```
portal/
├── app.py            application factory: blueprints, headers, error pages
├── config.py         every setting, read from the environment
├── auth.py           sessions, roles, /login, /logout, /_authcheck
├── users.py          the account store: load, save, verify, migrate
├── csrf.py           token minting and the before_request check
├── graph_store.py    the graph index on disk (locked, atomic)
├── refresh.py        the scheduled refresher (a timer runs this)
├── cli.py            portal-users
├── blueprints/
│   ├── gerrit_vis.py the main UI and the SocketIO handler
│   └── private_area.py  optional role-gated file listing
└── tools/
    ├── registry.py   tool registration
    ├── gc_graph.py   runs `gc graph`, streams output, classifies the result
    └── graph_stats.py parses generated HTML (no Flask, no config)
```

## Things that will bite you

**Nothing deployment-specific goes in the code.** Every path, URL,
project name and label comes from `config.py`, which reads the
environment and defaults to `./data`. If you find yourself typing a
hostname or an absolute path into a module, it belongs in config and in
`.env.example`.

**The public/internal split is the security boundary.** A graph belongs
to a project; anything other than `PORTAL_PUBLIC_PROJECT` needs the
`internal` role. Getting this wrong leaks the existence of internal
work, not just its contents. In particular:

- "no such graph" and "exists but you may not see it" must stay
  **byte-identical** 404s. There are tests asserting the response bodies
  match.
- A public graph must never get `--cross-project`, even for a privileged
  user: the generated HTML is world-readable and its embedded JSON would
  carry identifiers from other projects.
- `_internal_access` is derived from the session in
  `handle_start_tool` and popped before any client value is read. It is
  also popped before params are stored, so it can never be replayed from
  the index.

**`gc_graph.py` parses the last JSON line of `gc`'s stdout** to recover
the resolved ticket anchor. That is an undocumented contract with the
CLI, which is why `vendor/llm_tools` is a pinned submodule. If ticket
mode starts mis-labelling graphs, check whether that output changed.

**Never `git submodule update --recursive`.** That repo has a nested
submodule over SSH which fails for anyone without the right keys.

**Roles are resolved per request**, cached on the account file's mtime.
Do not reintroduce a login-time snapshot: revocation has to be
immediate, and there is a test for it.

**`eventlet.monkey_patch()` must run before anything imports `ssl`,
`socket` or `threading`.** It is the first thing in `portal/__main__.py`
for that reason. Moving it produces a server that blocks on its first
concurrent request, which looks like an unrelated bug.

**`graph_stats.py` has no Flask and no config imports** on purpose, so
one-off scripts can use it. Keep it that way; pass values in.

**nginx `add_header` does not merge.** A single `add_header` in a block
discards every header inherited from an outer one, which is why each
proxying location includes the full `portal-headers.conf`.

## Tests

```bash
.venv/bin/python -m pytest
```

Hermetic: no network, no Gerrit, no `gc`. Test behaviour through the
app, never by grepping source text — a previous version of this suite
did that and the tests broke on every rename while proving nothing.
`tests/helpers.py` has the fake subprocess; `conftest.py` has the
fixtures, including a test client that carries CSRF tokens.

## Conventions

- Comments say *why*. Several exist because the obvious approach was
  tried and broke something; do not delete them as redundant.
- New state-changing routes need CSRF and a role check.
- New settings: `config.py` **and** `.env.example`, both.
- The private area and the dashboard shell are optional and must stay
  that way — unset config means the route does not exist, not that it is
  hidden.
