"""Follow-ups found by plain Gerrit search, and where the search may look."""

from portal.promises import followups
from tests.promises_helpers import PUBLIC, change_doc, th


class FakeGerrit:
    def __init__(self, results=None, changes=None):
        self.queries = []
        self.results = results or {}
        self.changes = changes or {}

    def get_change(self, num, options=None):
        if num not in self.changes:
            raise RuntimeError("404")
        return self.changes[num]

    def search_all(self, query, max_results=500, page_size=100, options=None):
        self.queries.append(query)
        for key, value in self.results.items():
            if key in query:
                return value[:max_results]
        return []


def test_every_named_ticket_is_searched_and_the_changes_own():
    t = th(replies=["I will create a patch in LU-19999."])
    client = FakeGerrit(
        {
            "LU-19999": [
                {
                    "_number": 70001,
                    "subject": "LU-19999 mdt: fix",
                    "status": "MERGED",
                    "project": PUBLIC,
                }
            ]
        }
    )
    found = followups.find_candidates(
        client, change_doc(threads=[t]), public_project=PUBLIC, ticket_prefix="LU"
    )
    assert found["own_ticket"] == "LU-19548"
    assert set(found["by_ticket"]) == {"LU-19548", "LU-19999"}
    assert found["by_ticket"]["LU-19999"][0]["number"] == 70001


def test_ticket_searches_are_narrowed_on_the_server_not_cut_short():
    """after: the change's creation and no abandoned changes, instead of
    the newest few of everything -- a busy ticket used to lose its real
    follow-ups that way."""
    t = th(replies=["I will create a patch in LU-19999."])
    client = FakeGerrit(
        {"LU-19999": [{"_number": 70000 + i, "status": "NEW"} for i in range(60)]},
        changes={
            64620: {
                "_number": 64620,
                "created": "2026-03-17 08:00:00.000000000",
                "topic": "ec2",
                "hashtags": ["pt_ecro"],
            }
        },
    )
    found = followups.find_candidates(
        client, change_doc(threads=[t]), public_project=PUBLIC, ticket_prefix="LU"
    )
    ticket_queries = [q for q in client.queries if "LU-19999" in q]
    assert ticket_queries and all(
        "-is:abandoned" in q and "after:2026-03-17" in q for q in ticket_queries
    )
    assert len(found["by_ticket"]["LU-19999"]) == 60, "Gerrit's pages are followed"
    assert found["change"] == {
        "created": "2026-03-17 08:00:00.000000000",
        "topic": "ec2",
        "hashtags": ["pt_ecro"],
    }


def test_a_public_change_is_searched_within_the_public_project_only():
    """Otherwise a public page could list an internal change."""
    client = FakeGerrit()
    followups.find_candidates(client, change_doc(), public_project=PUBLIC, ticket_prefix="LU")
    assert client.queries and all(f"project:{PUBLIC}" in q for q in client.queries)


def test_an_internal_change_may_find_anything():
    client = FakeGerrit()
    followups.find_candidates(
        client,
        change_doc(project="internal/example-project"),
        public_project=PUBLIC,
        ticket_prefix="LU",
    )
    assert not any("project:" in q for q in client.queries)


def test_checker_threads_are_not_searched():
    t = th(replies=["LU-777 is mentioned by a bot"], mechanical=True)
    client = FakeGerrit()
    found = followups.find_candidates(
        client, change_doc(threads=[t]), public_project=PUBLIC, ticket_prefix="LU"
    )
    assert "LU-777" not in found["by_ticket"]


def test_a_merged_change_is_also_searched_by_its_commit():
    client = FakeGerrit()
    doc = change_doc(status="MERGED", revision="0123456789abcdef0123")
    followups.find_candidates(client, doc, public_project=PUBLIC, ticket_prefix="LU")
    assert any('message:"0123456789ab"' in q for q in client.queries)


def test_a_failed_search_does_not_stop_the_rest():
    class Broken(FakeGerrit):
        def search_all(self, query, **kw):
            raise RuntimeError("gerrit down")

    found = followups.find_candidates(
        Broken(), change_doc(), public_project=PUBLIC, ticket_prefix="LU"
    )
    assert found["by_ticket"] == {"LU-19548": []}


def test_changes_linked_in_a_thread_are_candidates_if_public():
    t = th(replies=["Addressed in https://review.whamcloud.com/c/fs/lustre-release/+/68697"])
    t["links"] = [68697, 70000, 99999]
    client = FakeGerrit(
        changes={
            68697: {
                "_number": 68697,
                "subject": "LU-19548 mdd: refuse",
                "status": "MERGED",
                "project": PUBLIC,
            },
            70000: {
                "_number": 70000,
                "subject": "secret",
                "status": "NEW",
                "project": "internal/example-project",
            },
        }
    )
    found = followups.find_candidates(
        client, change_doc(threads=[t]), public_project=PUBLIC, ticket_prefix="LU"
    )
    assert list(found["by_link"]) == ["68697"], "the internal one and the dead link are dropped"

    from portal.promises import status
    from tests.promises_helpers import classification, overrides_doc

    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)}, candidates=found)
    it = status.build_items(doc, overrides_doc())[0][0]
    assert [(c.number, c.status, c.reason) for c in it.candidates] == [
        (68697, "MERGED", "linked in the thread")
    ]


def test_stacked_changes_are_those_above_this_one_in_the_chain():
    """Gerrit lists the chain top-down: above, this one, below. Parent
    commits do not work -- children are rebased onto the merged commit."""

    class Chain(FakeGerrit):
        def get_related_changes(self, number):
            return [
                {
                    "_change_number": 67878,
                    "status": "NEW",
                    "_current_revision_number": 4,
                    "commit": {"subject": "LU-20566 ec: recover O_DIRECT reads"},
                },
                {"_change_number": 64079, "status": "ABANDONED", "commit": {"subject": "tests"}},
                {"_change_number": 62757, "status": "MERGED", "commit": {"subject": "self"}},
                {"_change_number": 60000, "status": "MERGED", "commit": {"subject": "below"}},
            ]

    got = followups.stacked_on(Chain(), 62757)
    assert [(c["number"], c["current_patchset"]) for c in got] == [(67878, 4)]
    assert followups.stacked_on(Chain(), 11111) == [], "not in its own chain: nothing"
