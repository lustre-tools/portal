# Contributing

Patches welcome. This is a small project, so there is not much process.

## Getting set up

```bash
git clone --recurse-submodules=no https://github.com/lustre-tools/portal.git
cd portal
git submodule update --init vendor/llm_tools     # not --recursive, see below
./install.sh --dev
```

That creates `.venv` with the portal, the `gc` CLI and the dashboard
installed editable. Then:

```bash
.venv/bin/portal-users set-password --user you
.venv/bin/portal-users add-role --user you --role admin
.venv/bin/portal
```

**Do not use `--recursive` on the submodule.** `llm_code_and_review_tools`
has a nested submodule over SSH which will fail for you, and nothing
here needs it.

## Tests

```bash
.venv/bin/python -m pytest
```

They are fast and hermetic — no network, no Gerrit, no `gc`. Each test
gets its own temporary data directory and account store, and the graph
tool runs against a fake subprocess.

Please add a test with a change. Some notes on what the existing ones
are trying to do:

- **Test behaviour, not source text.** An earlier version of this suite
  asserted on literal strings in the source. Those tests broke on every
  rename and never actually proved the behaviour held. If something is
  hard to test through the app, that is usually a sign the code wants
  rearranging.
- **The authorization tests are the important ones.**
  `tests/test_internal_authz.py` is the public/internal split end to
  end: list filtering, direct URLs, autocomplete, re-run data, the
  WebSocket, and the identical-404 rule. Anything touching visibility
  should add to it.
- `tests/test_deploy.py` renders the deploy templates and checks the
  things that only break on someone else's machine: unsubstituted
  placeholders, units writing outside their confinement, missing
  security headers.

## Style

Match the surrounding code. A few habits worth keeping:

- Comments explain *why*, especially where the obvious-looking thing is
  wrong. Several of them exist because the obvious thing was tried and
  broke something.
- New configuration goes through `portal/config.py` and gets an entry in
  `.env.example`. Nothing deployment-specific belongs in the code.
- New state-changing routes need a CSRF token, and a role check if they
  are not meant for everyone.

## Adding a tool

The portal is meant to host more than one.

1. Write `portal/tools/my_tool.py` with a `run_fn(params, socketio, room)`
   that emits `output` and `complete` events.
2. Register it with `register_tool(ToolDefinition(...))` — id, name,
   description and a list of `ToolParam`s.
3. Call your `init_*` function from `create_app()`.

The form, the streaming console and the run history come for free. Mark
a parameter `internal_only=True` to hide it from users without the role
— and enforce that server-side too, since hiding it in the UI is
cosmetic.

## Submitting

Open a pull request against `main`. Please say what problem the change
solves; a failing test that now passes is the clearest way to do that.

If you think you have found a security problem, do not open a public
issue — see [SECURITY.md](SECURITY.md).
