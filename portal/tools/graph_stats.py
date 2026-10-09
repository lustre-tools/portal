"""Extract stats from a generated graph HTML.

Deliberately dependency-free -- no Flask, no configuration -- so the
scheduled refresher and any one-off script can import it directly.
Anything deployment-specific (which project counts as public, which
accounts are CI voters) is passed in by the caller.
"""

import json
import re

#: CI accounts whose -1 on Verified is worth calling out separately.
DEFAULT_CI_VOTERS = ("maloo", "jenkins")


def review_health(node, ci_voters=DEFAULT_CI_VOTERS):
    """reviewHealth() of the graph page (gc graph's graph.js), in Python.

    The list must say what the graph says, so this follows the page rule
    for rule; tests/test_health_parity.py runs the page's own function
    from the bundled gc on the same nodes and fails when they disagree --
    the page's rule changes with gc, and this copy once fell behind it
    (the list showed 14 ready where the graph showed 7).

    Returns 'good', 'pending', 'bad_veto', 'bad_other', or
    'bad_<voter>' for a failing vote from one of ``ci_voters``.
    """
    if node.get("status") != "NEW":
        return "pending"
    rv = node.get("review") or {}
    if rv.get("cr_veto"):
        return "bad_veto"
    if rv.get("verified_fail"):
        fail_voters = [
            (v.get("name") or "").lower()
            for v in rv.get("verified_votes") or []
            if (v.get("value") or 0) < 0
        ]
        for voter in ci_voters:
            if voter.lower() in fail_voters:
                return f"bad_{voter.lower()}"
        return "bad_other"
    if rv.get("verified_pass"):
        # Every CI has to have voted +1: a run that never fired leaves no
        # vote at all, and one +1 alone counts as a pass.
        passers = {
            (v.get("name") or "").lower()
            for v in rv.get("verified_votes") or []
            if (v.get("value") or 0) > 0
        }
        if not all(voter.lower() in passers for voter in ci_voters):
            return "pending"
        # Gerrit's self-approval rule keys off the owner, not the author.
        owner = node.get("owner") or node.get("author") or ""
        non_owner_plus = sum(
            1
            for v in rv.get("cr_votes") or []
            if (v.get("value") or 0) > 0 and v.get("name") != owner
        )
        # A backport of a master change needs one reviewer, the rest two.
        if non_owner_plus >= (1 if node.get("is_backport") else 2):
            return "good"
    return "pending"


def _counted(node):
    """Whether a node counts in the graph's figures, as gc counts them
    (status_counts): an in-flight change the series only sits on (shown
    dimmed, "unrelated parent") does not, nor does a merged change that
    is only there to hold up the trunk."""
    if node.get("unrelated_parent"):
        return False
    return not (node.get("status") == "MERGED" and node.get("trunk_structural"))


_GRAPH_DATA_MARKER = re.compile(r"const\s+G\s*=\s*")


def _load_graph_data(html_path):
    """Parse the embedded `const G = {...}` payload from a graph HTML.

    The object is decoded with ``raw_decode`` from where it starts, so a
    ``};`` inside a string value (a commit subject, say) cannot cut it
    short the way a non-greedy regex would.

    Returns the decoded dict, or None if the file can't be read/parsed.
    """
    try:
        with open(html_path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    m = _GRAPH_DATA_MARKER.search(content)
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(content, m.end())
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def classify_graph_project(html_path, public_project, anchor_id=None):
    """Classify a generated graph by the projects of its nodes.

    A graph is "internal" if ANY node belongs to a non-public project, so
    this returns the public project only when every node is public (or
    carries no explicit project — legacy/fs output), otherwise the name of
    a non-public project present in the graph. When `anchor_id` is given,
    also returns that node's subject (for the entry's display name).

    Returns (project, anchor_subject). Returns (None, "") when the HTML
    can't be parsed so the caller can fall back to a safe default.
    """
    g = _load_graph_data(html_path)
    if g is None:
        return None, ""
    nodes = g.get("nodes", []) or []
    non_public = sorted(
        {n.get("project") for n in nodes if n.get("project") and n.get("project") != public_project}
    )
    project = non_public[0] if non_public else public_project

    anchor_subject = ""
    if anchor_id is not None:
        for n in nodes:
            if str(n.get("id")) == str(anchor_id):
                anchor_subject = n.get("subject", "") or ""
                break
    return project, anchor_subject


def extract_stats_from_html(html_path, ci_voters=DEFAULT_CI_VOTERS):
    """Read a generated graph HTML and return a stats dict.

    Returns dict with: node_count, inflight, ready, pending, blocked,
    merged, abandoned. Returns None if extraction fails.
    """
    g = _load_graph_data(html_path)
    if g is None:
        return None

    sc = (g.get("stats") or {}).get("status_counts", {})
    inflight = sc.get("NEW", 0)
    merged = sc.get("MERGED", 0)
    abandoned = sc.get("ABANDONED", 0)
    node_count = (g.get("stats") or {}).get("node_count", 0)

    ready = pending = blocked = 0
    for n in g.get("nodes", []):
        if n.get("status") != "NEW" or not _counted(n):
            continue
        h = review_health(n, ci_voters)
        if h == "good":
            ready += 1
        elif h == "pending":
            pending += 1
        else:
            blocked += 1

    return {
        "node_count": node_count,
        "inflight": inflight,
        "ready": ready,
        "pending": pending,
        "blocked": blocked,
        "merged": merged,
        "abandoned": abandoned,
    }


#: The parts of ``G.stats.summary`` the portal shows. ``last_30d`` and
#: ``prev_30d`` are left out on purpose: they are fixed at generation
#: time, and the list recounts them from ``recent_events`` at render
#: time instead, so a graph that has not been refreshed for a week does
#: not claim last week's numbers are this month's.
SUMMARY_KEYS = (
    "as_of",
    "patches",
    "open",
    "merged",
    "abandoned",
    "recent_events",
    "time_to_merge",
    "time_to_first_review",
    "patchsets_to_merge",
    "oldest_open",
    "longest_idle",
    "merged_by_month",
)


def extract_summary(html_path):
    """The graph's own summary (``G.stats.summary``), trimmed to what the
    list shows.

    Graphs from an engine that predates the summary return None, as do
    unreadable files; callers treat both as "no stats yet".
    """
    g = _load_graph_data(html_path)
    if g is None:
        return None
    summary = (g.get("stats") or {}).get("summary")
    if not isinstance(summary, dict):
        return None
    return {k: summary.get(k) for k in SUMMARY_KEYS if k in summary}


#: Longest subject kept per patch; the list shows one line of it.
SUBJECT_MAX = 120


def _block_reason(health, review):
    """A short "why is this blocked" for a review_health() verdict."""
    if health == "bad_veto":
        # cr_veto is any negative code review -- in practice nearly always
        # a -1. Only cr_rejected marks a real -2.
        return "Review −2" if (review or {}).get("cr_rejected") else "Review −1"
    if health == "bad_other":
        return "Verified −1"
    # bad_<voter>, one of the configured CI accounts.
    return f"{health[4:].capitalize()} −1"


def extract_patches(html_path, ci_voters=DEFAULT_CI_VOTERS):
    """Which patches are ready and which are blocked, plus the ids of
    every open and merged patch.

    The lists feed the expanded row ("what can land now?"); the id sets
    let a filtered view add graphs up without counting a patch twice
    when it belongs to two overlapping series.

    Returns None if the graph can't be read.
    """
    g = _load_graph_data(html_path)
    if g is None:
        return None

    ready, blocked, open_ids, merged_ids = [], [], [], []
    for n in g.get("nodes", []) or []:
        change = n.get("id")
        if not isinstance(change, int) or not _counted(n):
            continue
        status = n.get("status")
        if status == "MERGED":
            merged_ids.append(change)
            continue
        if status != "NEW":
            continue
        open_ids.append(change)
        health = review_health(n, ci_voters)
        subject = (n.get("subject") or "")[:SUBJECT_MAX]
        if health == "good":
            ready.append(
                {"id": change, "subject": subject, "last_activity": n.get("last_activity")}
            )
        elif health.startswith("bad_"):
            blocked.append(
                {
                    "id": change,
                    "subject": subject,
                    "reason": _block_reason(health, n.get("review")),
                }
            )

    return {
        "ready": sorted(ready, key=lambda p: p["id"]),
        "blocked": sorted(blocked, key=lambda p: p["id"]),
        "open_ids": sorted(open_ids),
        "merged_ids": sorted(merged_ids),
    }
