"""The list's review health is the graph page's.

The graph page decides what is ready in JavaScript (reviewHealth() in gc
graph's graph.js); the list decides it in Python (graph_stats.review_health),
and the two once drifted -- the list said 14 ready where the graph said 7.
This runs the page's own function, from the bundled gc, on the same nodes
as the portal's, so a change to the rule in gc fails here first.
"""

import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from portal.tools.graph_stats import review_health

GRAPH_JS = (
    Path(__file__).resolve().parents[1]
    / "vendor/llm_tools/gerrit_cli/gerrit_cli/graph/templates/graph.js"
)

pytestmark = [
    pytest.mark.skipif(not GRAPH_JS.exists(), reason="vendor/llm_tools is not checked out"),
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
]


def _function(source: str, name: str) -> str:
    """The source of `function name(...) {...}`, braces matched."""
    start = source.index(f"function {name}(")
    depth = 0
    for i in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        if depth == 0:
            return source[start : i + 1]
    raise ValueError(f"unbalanced {name}()")


def _nodes():
    """Every combination of what the rule looks at."""
    verified = {
        "none": [],
        "jenkins only": [{"name": "jenkins", "value": 1}],
        "both": [{"name": "jenkins", "value": 1}, {"name": "Maloo", "value": 1}],
        "maloo -1": [{"name": "jenkins", "value": 1}, {"name": "maloo", "value": -1}],
        "jenkins -1": [{"name": "Jenkins", "value": -1}],
        "other -1": [{"name": "jenkins", "value": 1}, {"name": "Janitor", "value": -1}],
    }
    reviews = {
        "none": [],
        "one": [{"name": "alice", "value": 1}],
        "two": [{"name": "alice", "value": 1}, {"name": "bob", "value": 1}],
        "owner and one": [{"name": "pat", "value": 1}, {"name": "alice", "value": 1}],
        "minus": [{"name": "alice", "value": 1}, {"name": "bob", "value": -1}],
    }
    for status, ver, rev, veto, backport, owner in itertools.product(
        ("NEW", "MERGED", "ABANDONED"),
        verified,
        reviews,
        (False, True),
        (False, True),
        ("pat", None),
    ):
        votes = verified[ver]
        node = {
            "status": status,
            "author": "alice",
            "is_backport": backport,
            "review": {
                "cr_veto": veto,
                "verified_votes": votes,
                "verified_pass": any(v["value"] > 0 for v in votes)
                and not any(v["value"] < 0 for v in votes),
                "verified_fail": any(v["value"] < 0 for v in votes),
                "cr_votes": reviews[rev],
            },
        }
        if owner:
            node["owner"] = owner
        yield node


def test_the_list_and_the_graph_page_agree_on_every_node():
    page_rule = _function(GRAPH_JS.read_text(), "reviewHealth")
    nodes = list(_nodes())
    script = page_rule + "\nconst nodes = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
    script += "process.stdout.write(JSON.stringify(nodes.map(reviewHealth)));\n"
    out = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(nodes),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    page = json.loads(out.stdout)
    differ = [
        (n, page[i], review_health(n)) for i, n in enumerate(nodes) if review_health(n) != page[i]
    ]
    assert not differ, f"{len(differ)} of {len(nodes)} differ, e.g. {differ[:3]}"
