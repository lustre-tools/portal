"""``portal-promises``: the Gerrit Promises pipelines from a shell.

    portal-promises scan 62757 70100      track + harvest + follow-ups (free)
    portal-promises classify 62757        find the promises (Claude)
    portal-promises judge 62757           check them (Claude)
    portal-promises export 62757 [-o f]   markdown
    portal-promises list
    portal-promises import DIR            gerrit-followup data -> here
    portal-promises mirror                fetch the code the checks read (the first
                                          fetch takes minutes; later ones seconds)

Configuration comes from the environment, as for the service. On a
server, run it with the service's environment and user, e.g.

    systemd-run --wait --pipe --collect -p User=portal -p Group=portal \\
        -p EnvironmentFile=/etc/portal/portal.env -p ProtectSystem=strict \\
        -p ReadWritePaths=/var/lib/portal /opt/portal/.venv/bin/portal-promises list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _progress(phase: str, detail: str = "") -> None:
    print(f"  [{phase}] {detail}".rstrip(), flush=True)


def _settings():
    # No sessions are signed here, so no secret key is needed.
    os.environ.setdefault("PORTAL_ALLOW_EPHEMERAL_SECRET", "1")
    from portal.config import build_config
    from portal.promises import claude, jobs
    from portal.promises.store import Store

    config = build_config()
    settings = jobs.Settings.from_config(config)
    claude.configure(settings.claude.parallel, settings.claude.min_free_mb)
    return config, settings, Store(settings.root)


def _parse(raw: str) -> int | None:
    from portal.blueprints.promises import parse_change_arg

    return parse_change_arg(raw)


def import_followup_data(source: Path, store, mechanical, force: bool = False) -> list[int]:
    """Copy gerrit-followup's documents in, re-deriving the fields it did
    not have (mechanical threads, the keyword preview)."""
    from portal.promises import harvest as harvest_mod

    mech = {m.lower() for m in mechanical}
    done = []
    for path in sorted((source / "changes").glob("*.json")):
        try:
            number = int(path.stem)
        except ValueError:
            continue
        if store.has_change(number) and not force:
            print(f"  {number}: already tracked, skipped (--force replaces it)")
            continue
        doc = json.loads(path.read_text())
        for t in (doc.get("harvest") or {}).get("threads") or []:
            authors = [(t.get("root") or {}).get("author", "")] + [
                r.get("author", "") for r in t.get("replies") or []
            ]
            messages = [(t.get("root") or {}).get("message", "")] + [
                r.get("message", "") for r in t.get("replies") or []
            ]
            t["mechanical"] = bool(authors) and all(a.lower() in mech for a in authors)
            t["keyword_hit"] = bool(
                any(harvest_mod.PROMISE_WORDS.search(m or "") for m in messages)
            )
        doc.setdefault("candidates", {})
        doc.setdefault("spend", {})
        doc["added_by"] = doc.get("added_by") or "imported"
        store.mutate_change(number, lambda d, new=doc: (d.clear(), d.update(new)))
        overrides = source / "overrides" / f"{number}.json"
        if overrides.exists():
            ov = json.loads(overrides.read_text())
            store.mutate_overrides(number, lambda d, new=ov: (d.clear(), d.update(new)))
        done.append(number)
        print(f"  {number}: imported")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="portal-promises", description="Gerrit Promises from the shell"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="track + harvest + follow-up search (free)")
    p.add_argument("changes", nargs="+")
    p = sub.add_parser("classify", help="find the promises (Claude)")
    p.add_argument("change")
    p = sub.add_parser("judge", help="check the promises against the code (Claude)")
    p.add_argument("change")
    p.add_argument("--scope", choices=("pending", "open"), default="pending")
    p.add_argument("--tid", default=None, help="one thread id")
    p = sub.add_parser("export", help="markdown for one change")
    p.add_argument("change")
    p.add_argument("-o", "--output")
    sub.add_parser("list", help="tracked changes and their counts")
    sub.add_parser("mirror", help="create or update the repository mirror the checks read")
    p = sub.add_parser("import", help="import a gerrit-followup data directory")
    p.add_argument("source", type=Path)
    p.add_argument("--force", action="store_true", help="replace changes that are already tracked")
    args = parser.parse_args(argv)

    config, settings, store = _settings()
    from portal.promises import export, jobs, status
    from portal.promises.models import sort_items, summarize

    model = settings.claude.judge_model

    if args.cmd == "mirror":
        from portal.promises import repo

        mirror = repo.ensure_mirror(
            settings.root / "repos", settings.gerrit_url, settings.public_project
        )
        print(f"fetching {settings.public_project} master")
        repo.fetch(mirror)
        print("master is at", repo.rev_parse(mirror, "refs/heads/master")[:12])
        return 0
    if args.cmd == "import":
        import_followup_data(args.source, store, settings.mechanical, force=args.force)
        return 0
    if args.cmd == "list":
        for number in store.tracked_changes():
            doc = store.load_change(number)
            items, _ = status.build_items(
                doc, store.load_overrides(number), model, settings.ticket_prefix
            )
            c = summarize(items)
            subject = ((doc.get("harvest") or {}).get("change") or {}).get("subject", "?")
            print(
                f"{number}  open={c['open']} check={c['attention']} kept={c['addressed']}  {subject}"
            )
        return 0

    numbers = [_parse(x) for x in (args.changes if args.cmd == "scan" else [args.change])]
    if not all(numbers):
        print("not a change number or Gerrit URL", file=sys.stderr)
        return 2
    if args.cmd == "scan":
        for n in numbers:
            print(f"scanning {n}")
            jobs.run_add(settings, store, n, progress=_progress, added_by="cli")
    elif args.cmd == "classify":
        jobs.run_classify(settings, store, numbers[0], progress=_progress)
    elif args.cmd == "judge":
        jobs.run_judge(
            settings, store, numbers[0], scope=args.scope, tid=args.tid, progress=_progress
        )
    elif args.cmd == "export":
        doc = store.load_change(numbers[0])
        items, _ = status.build_items(
            doc, store.load_overrides(numbers[0]), model, settings.ticket_prefix
        )
        text = export.render_markdown(doc, sort_items(items))
        if args.output:
            Path(args.output).write_text(text)
        else:
            print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
