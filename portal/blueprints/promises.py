"""Gerrit Promises: promises made in code review, and whether they were kept.

Who may do what:

* anyone -- see the tracked changes of the public project and their
  promises; internal changes need the internal role, and are otherwise
  the same 404 as a change that is not tracked;
* signed in -- track a change and rescan it. Both are free: harvesting,
  the keyword preview and the follow-up search use no AI;
* admin -- everything that runs Claude (finding and checking promises),
  deciding an item by hand, manual items, untracking.

The pipelines live in :mod:`portal.promises.jobs`; this module is routes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from portal.auth import (
    can_view_internal,
    current_user,
    is_admin,
    is_authenticated,
    require_admin,
    require_auth,
)
from portal.promises import export as export_mod
from portal.promises import followups, harvest, jobs, status
from portal.promises.models import (
    BUCKET_LABELS,
    BUCKET_ORDER,
    STATUS_LABELS,
    Followup,
    now_iso,
    sort_items,
    summarize,
)
from portal.promises.store import Store

promises_bp = Blueprint("promises", __name__)

_CHANGE_RE = re.compile(r"/\+/(\d+)|^(\d{2,8})$|/(\d{2,8})/?$")


def parse_change_arg(raw: str) -> int | None:
    """A change number from "62757", ".../c/fs/lustre-release/+/62757"
    or "https://review.whamcloud.com/62757"."""
    raw = (raw or "").strip().rstrip("/")
    m = _CHANGE_RE.search(raw)
    if not m:
        return None
    return int(next(g for g in m.groups() if g))


def _ext():
    return current_app.extensions["promises"]


def _settings() -> jobs.Settings:
    return _ext()["settings"]


def _store() -> Store:
    return _ext()["store"]


def _registry() -> jobs.JobRegistry:
    return _ext()["registry"]


def _public_project() -> str:
    return current_app.config["PUBLIC_PROJECT"]


def _project_of(doc: dict) -> str:
    return ((doc.get("harvest") or {}).get("change") or {}).get("project") or ""


def _visible(doc: dict) -> bool:
    """A change nobody has harvested yet has no project; treat it as
    internal until it is known, so it cannot leak through the list."""
    project = _project_of(doc)
    return project == _public_project() or can_view_internal()


def _load_visible(number: int) -> dict:
    """The change document, or a 404 -- identical whether the change is
    not tracked or only not visible to this session."""
    store = _store()
    if not store.has_change(number):
        abort(404)
    doc = store.load_change(number)
    if not _visible(doc):
        abort(404)
    return doc


def _prefix() -> str:
    return current_app.config["TICKET_PREFIX"]


def _change_view(number: int, doc: dict | None = None) -> dict:
    settings = _settings()
    store = _store()
    doc = doc or store.load_change(number)
    overrides = store.load_overrides(number)
    model = settings.claude.judge_model if settings.claude else ""
    all_items, non_items = status.build_items(doc, overrides, model, _prefix())
    # Every promise once: duplicates are folded into their primary.
    items = sort_items(status.top_level(all_items))
    grouped = {b: [it for it in items if it.bucket == b] for b in BUCKET_ORDER}
    harvested = doc.get("harvest") or {}
    candidates = doc.get("candidates") or {}
    own = candidates.get("own_ticket") or ""
    return {
        "number": number,
        "doc": doc,
        "change": harvested.get("change") or {},
        "harvest": harvested,
        "promises": items,
        "all_items": all_items,
        "groups_stale": status.groups_stale(doc, all_items),
        "duplicate_count": sum(1 for it in all_items if it.duplicate_of),
        "grouped": grouped,
        "non_items": [
            it
            for it in non_items
            if it.tid not in {p.tid for p in status.possible_promises(doc, non_items)}
        ],
        "possible": status.possible_promises(doc, non_items),
        "counts": summarize(items),
        "resolved_open": sum(1 for it in items if it.resolved_but_open),
        "unclassified": status.unclassified_count(doc),
        "classified_any": bool(doc.get("classifications")),
        "to_check": len(status.pending_and_stale_tids(doc, overrides, model, _prefix())),
        "judge_unsupported": jobs.judge_unsupported(settings, doc),
        "own_ticket": own,
        "own_ticket_changes": [
            Followup(**_fu(c)) for c in (candidates.get("by_ticket") or {}).get(own, [])
        ],
        "fixes": [Followup(**_fu(c)) for c in candidates.get("fixes") or []],
        "threads_total": len(harvested.get("threads") or []),
        "threads_mechanical": sum(1 for t in harvested.get("threads") or [] if t.get("mechanical")),
        "spend": doc.get("spend") or {},
        "job": _registry().snapshot().get(str(number)),
        "internal": _project_of(doc) != _public_project(),
    }


def _fu(c: dict) -> dict:
    return {
        "number": int(c.get("number") or 0),
        "status": c.get("status", ""),
        "subject": c.get("subject", ""),
        "current_patchset": c.get("current_patchset"),
    }


@promises_bp.app_template_filter("promise_ago")
def promise_ago(stamp: str | None) -> str:
    """ "3 d ago" for our timestamps ("2026-08-31T18:33:00Z") and Gerrit's
    ("2026-08-28 13:56:46.000000000")."""
    from portal.stats_view import fmt_ago

    if not stamp:
        return "never"
    text = stamp.replace("T", " ").rstrip("Z").split(".")[0]
    try:
        then = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return stamp[:10]
    return fmt_ago((datetime.now(UTC) - then).total_seconds())


def _month_start() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-01T00:00:00Z")


def _common():
    settings = _settings()
    return {
        "bucket_order": BUCKET_ORDER,
        "bucket_labels": BUCKET_LABELS,
        "status_labels": STATUS_LABELS,
        "decision_labels": {
            "done": "kept",
            "open": "still open",
            "not_needed": "not needed",
            "ignored": "ignored",
        },
        "authenticated": is_authenticated(),
        "is_admin": is_admin(),
        "claude_ready": bool(settings.claude and settings.claude.available),
        "public_project": _public_project(),
    }


# ------------------------------------------------------------- pages


@promises_bp.get("/")
def index():
    store = _store()
    query = (request.args.get("q") or "").strip().lower()
    view = request.args.get("view", "changes")
    rows, open_items = [], []
    for number in store.tracked_changes():
        doc = store.load_change(number)
        if not _visible(doc):
            continue
        v = _change_view(number, doc)
        if query:
            hay = " ".join(
                [
                    str(number),
                    v["change"].get("subject", ""),
                    v["change"].get("owner", ""),
                    doc.get("added_by") or "",
                ]
                + [f"{it.promised_by or ''} {it.summary}" for it in v["all_items"]]
            ).lower()
            if query not in hay:
                continue
        rows.append(v)
        open_items += [it for it in v["promises"] if it.bucket == "open"]
    rows.sort(key=lambda v: v["harvest"].get("harvested_at") or "", reverse=True)
    totals = {b: sum(v["counts"][b] for v in rows) for b in BUCKET_ORDER}
    totals["resolved_open"] = sum(v["resolved_open"] for v in rows)
    spend = store.spend_since(_month_start()) if is_admin() else None
    return render_template(
        "promises/index.html",
        rows=rows,
        open_items=sort_items(open_items),
        totals=totals,
        spend_month=spend,
        query=request.args.get("q", ""),
        view=view if view in ("changes", "open") else "changes",
        msg=request.args.get("msg"),
        info=request.args.get("info"),
        **_common(),
    )


@promises_bp.get("/<int:number>")
def change_page(number: int):
    doc = _load_visible(number)
    return render_template(
        "promises/change.html",
        v=_change_view(number, doc),
        msg=request.args.get("msg"),
        **_common(),
    )


@promises_bp.get("/<int:number>/export.md")
def export_md(number: int):
    doc = _load_visible(number)
    v = _change_view(number, doc)
    return Response(export_mod.render_markdown(doc, v["promises"]), mimetype="text/markdown")


@promises_bp.get("/api/jobs")
def api_jobs():
    """Running and recent jobs, for the changes this session may see."""
    store = _store()
    out = {}
    for num, job in _registry().snapshot().items():
        doc = store.load_change(int(num))
        if store.has_change(int(num)) and not _visible(doc):
            continue
        if not store.has_change(int(num)) and not can_view_internal():
            # A change being tracked right now has no project yet.
            continue
        out[num] = job
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ----------------------------------------------------------- actions


def _back(number: int | None = None, msg: str | None = None, anchor: str = ""):
    if number is None:
        return redirect(
            url_for("promises.index", msg=msg) if msg else url_for("promises.index"), code=303
        )
    target = (
        url_for("promises.change_page", number=number, msg=msg)
        if msg
        else url_for("promises.change_page", number=number)
    )
    return redirect(target + anchor, code=303)


def _start(number: int, name: str, fn, back_to_change: bool = True):
    if not _registry().start(number, name, fn):
        return _back(number if back_to_change else None, "a job is already running for this change")
    return _back(number if back_to_change else None)


@promises_bp.post("/track")
@require_auth
def track():
    number = parse_change_arg(request.form.get("change", ""))
    if not number:
        return _back(None, "not a change number or Gerrit URL")
    store = _store()
    if store.has_change(number):
        if not _visible(store.load_change(number)):
            return _back(None, f"change {number} is not available")
        return _back(number)
    settings = _settings()
    # Access check before anything is stored. A change outside the public
    # project, for a session without the internal role, gets the same
    # answer as one that does not exist.
    try:
        info = harvest.change_project(settings.gerrit_url, number)
    except Exception:  # noqa: BLE001
        return _back(None, f"change {number} is not available")
    if info["project"] != settings.public_project and not can_view_internal():
        return _back(None, f"change {number} is not available")
    who = current_user()
    started = _registry().start(
        number,
        "track",
        lambda progress, cancel: jobs.run_add(
            settings, store, number, progress=progress, cancel=cancel, added_by=who
        ),
    )
    if not started:
        return _back(None, "a job is already running for this change")
    info = f"Tracking {number}: reading its threads, this takes a minute."
    return redirect(url_for("promises.index", info=info), code=303)


@promises_bp.post("/<int:number>/rescan")
@require_auth
def rescan(number: int):
    _load_visible(number)
    settings, store = _settings(), _store()
    return _start(
        number,
        "rescan",
        lambda progress, cancel: jobs.run_rescan(
            settings, store, number, progress=progress, cancel=cancel
        ),
    )


@promises_bp.post("/<int:number>/classify")
@require_admin
def classify(number: int):
    _load_visible(number)
    settings, store = _settings(), _store()
    return _start(
        number,
        "find promises",
        lambda progress, cancel: jobs.run_classify(
            settings, store, number, progress=progress, cancel=cancel
        ),
    )


@promises_bp.post("/<int:number>/judge")
@require_admin
def judge_change(number: int):
    _load_visible(number)
    settings, store = _settings(), _store()
    scope = "open" if request.form.get("scope") == "open" else "pending"
    depth = _depth()
    return _start(
        number,
        "check promises deeper" if depth == "deep" else "check promises",
        lambda progress, cancel: jobs.run_judge(
            settings, store, number, scope=scope, progress=progress, cancel=cancel, depth=depth
        ),
    )


def _depth() -> str:
    return "deep" if request.form.get("depth") == "deep" else "normal"


@promises_bp.post("/<int:number>/item/<tid>/judge")
@require_admin
def judge_item(number: int, tid: str):
    _load_visible(number)
    settings, store = _settings(), _store()
    depth = _depth()
    return _start(
        number,
        "check promise deeper" if depth == "deep" else "check promise",
        lambda progress, cancel: jobs.run_judge(
            settings, store, number, tid=tid, progress=progress, cancel=cancel, depth=depth
        ),
    )


@promises_bp.post("/<int:number>/item/<tid>/override")
@require_admin
def override_item(number: int, tid: str):
    doc = _load_visible(number)
    store, settings = _store(), _settings()
    form = request.form
    action = form.get("action", "save")
    raw = form.get("followup_change", "").strip()
    followup = None
    if raw:
        followup = parse_change_arg(raw)
        if followup is None:
            # A typo must never silently clear an existing link.
            return _back(number, "follow-up: not a change number or Gerrit URL", f"#item-{tid}")
    linked = {}
    if followup and str(followup) not in (doc.get("linked_changes") or {}):
        try:
            linked = followups.fetch_linked_changes(settings.gerrit(), [followup])
        except Exception:  # noqa: BLE001 - a Gerrit hiccup must not block the save
            linked = {}
    if followup and _project_of(doc) == settings.public_project:
        project = (
            linked.get(str(followup)) or (doc.get("linked_changes") or {}).get(str(followup)) or {}
        ).get("project")
        if not project:
            # Unverified is refused: a later rescan would publish whatever it is.
            return _back(
                number, f"could not look up change {followup} in Gerrit; try again", f"#item-{tid}"
            )
        if project != settings.public_project:
            # The page is public; a link would publish the other change.
            return _back(
                number,
                f"a public change can only link follow-ups in {settings.public_project}",
                f"#item-{tid}",
            )

    known = {t.get("id") for t in (doc.get("harvest") or {}).get("threads") or []}
    cls = (doc.get("classifications") or {}).get(tid) or {}

    # "Duplicate of": an item id as shown ("64620-a08dac95") or a thread id.
    dup_target, dup_given = None, "duplicate_of" in form
    if (form.get("duplicate_of") or "").strip():
        raw_dup = form["duplicate_of"].strip()
        ids = {
            it.item_id: it.tid
            for it in _change_view(number, doc)["all_items"]
            if it.origin == "thread"
        }
        dup_target = ids.get(raw_dup) or (raw_dup if raw_dup in known else None)
        if not dup_target or dup_target == tid:
            return _back(
                number,
                f"duplicate of: {raw_dup} is not another promise of this change",
                f"#item-{tid}",
            )

    def edit(ov: dict) -> None:
        if tid in ov["manual_items"]:
            entry = ov["manual_items"][tid]
            if action == "clear":
                entry["status"] = None
            elif form.get("status") in status.DECISIONS:
                entry["status"] = form["status"]
            for key in ("note", "summary"):
                if key in form:
                    entry[key] = form.get(key, "").strip()
            if "severity" in form:
                entry["severity"] = form.get("severity") or None
            entry["followup_change"] = followup
            return
        if action == "clear":
            ov["overrides"].pop(tid, None)
            return
        if tid not in known and tid not in ov["overrides"]:
            return  # neither a harvested thread nor a manual item
        entry = ov["overrides"].setdefault(tid, {})
        if form.get("status") in status.DECISIONS:
            entry["status"] = form["status"]
        else:
            entry.setdefault("status", None)
        # Stored only when they differ from the classification, so a
        # status click does not freeze today's machine summary.
        for key in ("note", "summary"):
            if key in form:
                val = form.get(key, "").strip() or None
                if key == "summary" and val == (cls.get("summary") or None):
                    val = None
                entry[key] = val
        if "severity" in form:
            val = form.get("severity") or None
            entry["severity"] = None if val == cls.get("severity") else val
        if "followup_change" in form or action == "save":
            entry["followup_change"] = followup
        if action == "not_duplicate":
            entry["not_duplicate"] = True
            entry.pop("duplicate_of", None)
        elif dup_given:
            entry["duplicate_of"] = dup_target
            if dup_target:
                entry.pop("not_duplicate", None)
        entry["set_at"] = now_iso()
        entry["set_by"] = current_user()

    store.mutate_overrides(number, edit)
    if linked:
        store.mutate_change(
            number, lambda d: d.setdefault("linked_changes", {}).update(linked), create=False
        )
    return _back(number, anchor=f"#item-{tid}")


@promises_bp.post("/<int:number>/item/add")
@require_admin
def add_manual_item(number: int):
    _load_visible(number)
    summary = request.form.get("summary", "").strip()
    if not summary:
        return _back(number, "a summary is required")
    line = request.form.get("line", "").strip()

    def edit(ov: dict) -> None:
        mid = f"m{ov['next_manual_id']}"
        ov["next_manual_id"] += 1
        ov["manual_items"][mid] = {
            "summary": summary[:300],
            "file_path": request.form.get("file_path", "").strip() or None,
            "line": int(line) if line.isdigit() else None,
            "severity": request.form.get("severity") or None,
            "note": request.form.get("note", "").strip(),
            "followup_change": None,
            "status": None,
            "created_at": now_iso(),
            "created_by": current_user(),
        }

    _store().mutate_overrides(number, edit)
    return _back(number)


@promises_bp.post("/<int:number>/item/<tid>/delete")
@require_admin
def delete_manual_item(number: int, tid: str):
    _load_visible(number)
    _store().mutate_overrides(number, lambda ov: ov["manual_items"].pop(tid, None))
    return _back(number)


@promises_bp.post("/<int:number>/cancel")
@require_admin
def cancel_job(number: int):
    """Stop a running job: no new Claude calls start, running ones are
    killed, and what finished is kept."""
    _load_visible(number)
    if not _registry().cancel(number):
        return _back(number, "nothing is running for this change")
    return _back(number)


@promises_bp.post("/<int:number>/untrack")
@require_admin
def untrack(number: int):
    _load_visible(number)
    if _registry().running(number):
        return _back(number, "a job is running for this change")
    _store().archive_change(number)
    return _back(None, f"stopped tracking {number}; tracking it again restores everything")


def init_promises(app) -> None:
    """Register the blueprint when the feature is switched on."""
    if not app.config.get("PROMISES_ENABLED"):
        return
    settings = jobs.Settings.from_config(app.config)
    from portal.promises import claude, repo

    claude.configure(settings.claude.parallel, settings.claude.min_free_mb)
    # A check killed by a restart leaves its workspace behind.
    repo.sweep(settings.root / "work")
    app.extensions["promises"] = {
        "settings": settings,
        "store": Store(settings.root),
        "registry": jobs.JobRegistry(),
    }
    app.register_blueprint(promises_bp, url_prefix="/gerrit_promise")
