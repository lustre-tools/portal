"""Turn a graph's stored summary into what the graph list shows.

Everything relative -- "merged in the last 30 days", "idle for 3 weeks"
-- is worked out here, at render time, from the absolute timestamps the
summary carries. A graph that has not been regenerated for a while
therefore still shows figures that are true today, rather than the ones
that were true when it was generated.

Pure functions of ``(entry, now)``; no Flask, so they test directly.
"""

import calendar
from datetime import datetime

DAY = 86400

#: Shown wherever a figure is missing, null, or cannot be worked out.
MISSING = "—"


def fmt_duration(seconds):
    """A duration in the largest unit that keeps it readable.

    ``47 h``, ``2.0 d``, ``9.9 d``, ``89 d``, ``2.9 mo``, ``2.0 y``.
    """
    if seconds is None:
        return MISSING
    h = seconds / 3600
    if h < 48:
        return f"{max(0, round(h))} h"
    d = seconds / DAY
    if d < 10:
        return f"{d:.1f} d"
    if d < 90:
        return f"{round(d)} d"
    if d < 730:
        return f"{d / 30.44:.1f} mo"
    return f"{d / 365.25:.1f} y"


def fmt_ago(seconds):
    """How long ago, for a timestamp: ``just now``, ``25 min ago``,
    ``5 h ago``, ``3 d ago``, ``4 mo ago``."""
    if seconds is None:
        return MISSING
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)} h ago"
    days = seconds / DAY
    if days < 60:
        return f"{int(days)} d ago"
    return f"{int(days / 30.44)} mo ago"


def fmt_count(value):
    """A median can be fractional (24.5); a whole one prints without ".0"."""
    if value is None:
        return MISSING
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.1f}"
    return str(int(value))


def _count_window(events, now, newest_days, oldest_days):
    """Events in the half-open window (now - oldest, now - newest].

    Half-open so that an event exactly 30 days old is counted once, in
    the older window, never in both.
    """
    hi = now - newest_days * DAY
    lo = now - oldest_days * DAY
    return sum(1 for t in events or () if lo < t <= hi)


def merged_trend(summary, now):
    """Merges in the last 30 days and in the 30 before that."""
    ev = ((summary or {}).get("recent_events") or {}).get("merged") or []
    return _count_window(ev, now, 0, 30), _count_window(ev, now, 30, 60)


def _num(value):
    return value if isinstance(value, (int, float)) else None


def bars(values, width, height, gap=1, floor=1):
    """Geometry for a small bar chart, bottom-aligned, in SVG units.

    Zero-height bars get ``floor`` units so an empty month still reads
    as a month rather than a hole.
    """
    n = len(values)
    if not n:
        return []
    top = max(values) or 1
    w = (width - gap * (n - 1)) / n
    out = []
    for i, v in enumerate(values):
        h = max(floor, round(height * v / top, 1)) if v else floor
        out.append(
            {
                "x": round(i * (w + gap), 2),
                "y": round(height - h, 2),
                "w": round(w, 2),
                "h": h,
                "value": v,
                "empty": not v,
            }
        )
    return out


def _month_label(ym):
    """``"2026-09"`` -> ``"Sep 2026"``; anything unexpected passes through."""
    try:
        y, m = ym.split("-")
        return f"{calendar.month_abbr[int(m)]} {y}"
    except (ValueError, IndexError, AttributeError):
        return str(ym)


def _generated_epoch(entry):
    """When the graph was generated, as an epoch, for sorting.

    ``generated_at`` is display text ("2026-09-24 06:09 PM CEST"); the
    zone name is dropped, which is close enough to order a list.
    """
    raw = entry.get("generated_at") or ""
    try:
        return datetime.strptime(raw.rsplit(" ", 1)[0], "%Y-%m-%d %I:%M %p").timestamp()
    except ValueError:
        return 0


def _stale_patch(item, key, now):
    """``{id, ticket, age, age_s}`` for oldest_open / longest_idle."""
    if not isinstance(item, dict) or _num(item.get(key)) is None:
        return None
    # The id goes into a Gerrit URL; accept a change number and nothing else.
    change = item.get("id")
    change = int(change) if str(change).isdigit() else None
    age = now - item[key]
    return {
        "id": change,
        "ticket": item.get("ticket") or "",
        "age": fmt_duration(age),
        "age_s": age,
    }


#: How many ready / blocked patches the expanded row lists before
#: pointing at the graph for the rest.
LIST_LIMIT = 8


def _patch_lists(entry, now):
    """The ready and blocked lists for the expanded row, trimmed."""
    p = entry.get("patches")
    if not isinstance(p, dict):
        return {"has_lists": False}

    def clean(items):
        out = []
        for item in items or ():
            if not isinstance(item, dict) or not str(item.get("id")).isdigit():
                continue
            out.append(item)
        return out

    ready = clean(p.get("ready"))
    blocked = clean(p.get("blocked"))
    return {
        "has_lists": True,
        "ready_list": [
            {
                "id": int(x["id"]),
                "subject": x.get("subject") or "",
                "idle": fmt_duration(now - x["last_activity"])
                if _num(x.get("last_activity")) is not None
                else None,
            }
            for x in ready[:LIST_LIMIT]
        ],
        "ready_more": max(0, len(ready) - LIST_LIMIT),
        "blocked_list": [
            {"id": int(x["id"]), "subject": x.get("subject") or "", "reason": x.get("reason") or ""}
            for x in blocked[:LIST_LIMIT]
        ],
        "blocked_more": max(0, len(blocked) - LIST_LIMIT),
    }


def group_totals(entries, now):
    """Figures for a whole filtered list -- every graph with a label, say.

    Series overlap: a patch can sit in two graphs. Where the graphs
    carry patch ids, each patch is counted once; merges are matched by
    their timestamp, which is the same in every graph that has them.
    Graphs from before the ids were stored fall back to their plain
    counts, and ``approx`` says so.
    """
    ready, blocked, open_, merged = set(), set(), set(), set()
    fallback = {"ready": 0, "blocked": 0, "open": 0, "merged": 0, "in_review": 0}
    approx = False
    merged_events = set()
    any_summary = False

    def ids(items):
        return {
            int(x["id"] if isinstance(x, dict) else x)
            for x in items or ()
            if str(x["id"] if isinstance(x, dict) else x).isdigit()
        }

    for e in entries:
        p = e.get("patches")
        if isinstance(p, dict):
            ready |= ids(p.get("ready"))
            blocked |= ids(p.get("blocked"))
            open_ |= ids(p.get("open_ids"))
            merged |= ids(p.get("merged_ids"))
        else:
            st = e.get("stats") or {}
            if st:
                approx = True
                fallback["ready"] += st.get("ready") or 0
                fallback["blocked"] += st.get("blocked") or 0
                fallback["open"] += st.get("inflight") or 0
                fallback["merged"] += st.get("merged") or 0
                fallback["in_review"] += st.get("pending") or 0
        s = e.get("summary")
        if isinstance(s, dict):
            any_summary = True
            merged_events.update(
                t
                for t in ((s.get("recent_events") or {}).get("merged") or ())
                if _num(t) is not None
            )

    cur = _count_window(merged_events, now, 0, 30)
    prev = _count_window(merged_events, now, 30, 60)
    return {
        "graphs": len(entries),
        "ready": len(ready) + fallback["ready"],
        "blocked": len(blocked) + fallback["blocked"],
        "open": len(open_) + fallback["open"],
        "merged": len(merged) + fallback["merged"],
        "in_review": len(open_ - ready - blocked) + fallback["in_review"],
        "has_trend": any_summary,
        "merged_30d": cur,
        "merged_prev_30d": prev,
        "trend": "up" if cur > prev else "down" if cur < prev else "flat",
        "approx": approx,
    }


def entry_view(entry, now):
    """Everything the list needs to show one graph, formatted.

    ``has_summary`` is False for graphs generated before summaries
    existed; the template then falls back to the older counts.
    """
    stats = entry.get("stats") or {}
    s = entry.get("summary")
    view = {
        "has_summary": isinstance(s, dict),
        "generated_epoch": _generated_epoch(entry),
        "ready": _num(stats.get("ready")),
        "in_review": _num(stats.get("pending")),
        "blocked": _num(stats.get("blocked")),
        **_patch_lists(entry, now),
    }
    if not view["has_summary"]:
        view.update(
            {
                "open": _num(stats.get("inflight")),
                "merged": _num(stats.get("merged")),
                "abandoned": _num(stats.get("abandoned")),
                "patches": _num(stats.get("node_count")),
            }
        )
        return view

    cur, prev = merged_trend(s, now)
    events = s.get("recent_events") or {}
    ttm = s.get("time_to_merge") or {}
    ttfr = s.get("time_to_first_review") or {}
    ps = s.get("patchsets_to_merge") or {}
    months = [
        (m[0], m[1])
        for m in (s.get("merged_by_month") or [])
        if isinstance(m, (list, tuple)) and len(m) == 2 and _num(m[1]) is not None
    ]

    patches = _num(s.get("patches"))
    parts = {k: _num(s.get(k)) for k in ("open", "merged", "abandoned")}
    total = sum(v for v in parts.values() if v) or 0

    as_of = _num(s.get("as_of"))
    view.update(
        {
            "patches": patches,
            **parts,
            # Share of each state, for the composition bar.
            "share": {k: (100 * v / total if total and v else 0) for k, v in parts.items()},
            "merged_30d": cur,
            "merged_prev_30d": prev,
            "trend": "up" if cur > prev else "down" if cur < prev else "flat",
            "opened_30d": _count_window(events.get("opened"), now, 0, 30),
            "abandoned_30d": _count_window(events.get("abandoned"), now, 0, 30),
            "ttm_median": fmt_duration(_num(ttm.get("median"))),
            "ttm_median_s": _num(ttm.get("median")),
            "ttm_p90": fmt_duration(_num(ttm.get("p90"))),
            "ttm_count": _num(ttm.get("count")) or 0,
            "ttfr_median": fmt_duration(_num(ttfr.get("median"))),
            "ttfr_p90": fmt_duration(_num(ttfr.get("p90"))),
            "ttfr_count": _num(ttfr.get("count")) or 0,
            "ps_median": fmt_count(_num(ps.get("median"))),
            "ps_max": fmt_count(_num(ps.get("max"))),
            "oldest_open": _stale_patch(s.get("oldest_open"), "opened_at", now),
            "longest_idle": _stale_patch(s.get("longest_idle"), "last_activity", now),
            "months": [
                {"label": _month_label(ym), "short": _month_label(ym)[:1], "value": v}
                for ym, v in months
            ],
            "spark": bars([v for _, v in months], 48, 14),
            "chart": bars([v for _, v in months], 240, 64, gap=4, floor=1.5),
            "month_max": max((v for _, v in months), default=0),
            "as_of_ago": fmt_ago(now - as_of) if as_of is not None else None,
        }
    )
    if as_of is not None:
        view["generated_epoch"] = as_of
    return view
