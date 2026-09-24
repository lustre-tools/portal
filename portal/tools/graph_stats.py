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
    """Port of reviewHealth() from the graph HTML's JS.

    Returns 'good', 'pending', 'bad_veto', 'bad_other', or
    'bad_<voter>' for a failing vote from one of ``ci_voters``.
    """
    if node.get("status") != "NEW":
        return "pending"
    rv = node.get("review", {}) or {}
    if rv.get("cr_veto"):
        return "bad_veto"
    if rv.get("verified_fail"):
        fail_voters = [
            (v.get("name") or "").lower()
            for v in rv.get("verified_votes", [])
            if v.get("value", 0) < 0
        ]
        for voter in ci_voters:
            if voter.lower() in fail_voters:
                return f"bad_{voter.lower()}"
        return "bad_other"
    if rv.get("verified_pass"):
        author = node.get("author", "")
        non_author_plus = sum(
            1 for v in rv.get("cr_votes", []) if v.get("value", 0) > 0 and v.get("name") != author
        )
        if non_author_plus >= 2:
            return "good"
    return "pending"


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
        if n.get("status") != "NEW":
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
