"""What a check looks for with the code at hand: the files a promise is
about, the tickets a change leaves in its code, and the searches."""

from portal.promises import discover, status
from tests.promises_helpers import PUBLIC, change_doc, classification, th

NORMAL = discover.DEPTHS["normal"]
DEEP = discover.DEPTHS["deep"]

# The shape of 62757: the AIO tests parked under another ticket in a test
# script, and a promise about them made on the commit message.
DIFFS = {
    "lustre/tests/sanity-ec.sh": "\n".join(
        [
            "-always_except LU-19631 12a",
            "+always_except LU-19631 12a",
            "+always_except LU-20566 41j 41k 41l 41m 41n",
            "+test_41j() {",
        ]
    ),
    "lustre/llite/file.c": "+\tif (ll_file_nolock(file))\n+\t\treturn -EIO; /* LU-12669 */",
    "lustre/lov/lov_io.c": "+\trc = lov_io_ec_recover(env, lio);\n" * 40,
}


def aio_item(**kw):
    t = th(
        tid="91cfac09_e607d390",
        file_path="/COMMIT_MSG",
        line=50,
        root_msg="those AIO tests are added to the always_except list.",
        replies=[],
        **kw,
    )
    cls = classification(
        t,
        summary="Re-enable the AIO tests currently parked in the always_except list",
        quote=None,
    )
    doc = change_doc(
        number=62757,
        threads=[t],
        classifications={t["id"]: cls},
        subject="LU-12669 ec: recover data from parity",
    )
    return status.build_items(doc, {"overrides": {}, "manual_items": {}})[0][0]


def test_identifiers_are_the_words_that_look_like_code():
    got = discover.identifiers(
        "LGTM. Those AIO tests are added to the always_except list; see "
        "lov_io_ec_recover() and sanity-ec 41j, maybe ioCount too, with 64k "
        "pages. TODO later."
    )
    assert {"AIO", "always_except", "lov_io_ec_recover", "41j", "ioCount"} <= got
    assert not {"LGTM", "TODO", "Those", "tests", "later", "64k"} & got


def test_a_test_number_matches_as_a_word_only():
    ids = {"41d"}
    assert discover.hits(ids, "test_41d() {") == ["41d"]
    assert discover.hits(ids, "always_except LU-20708 41d 53a") == ["41d"]
    assert discover.hits(ids, "0x41dead") == [] and discover.hits(ids, "141d") == []


def test_acronyms_alone_are_not_a_match():
    assert not discover.strong(["OST", "FLR"])
    assert discover.strong(["OST", "41d"]) and discover.strong(["always_except"])


def test_a_commit_message_comment_gets_the_file_its_words_are_in():
    item = aio_item()
    idents = discover.identifiers(discover.item_text(item))
    files = discover.files_for(item, DIFFS, idents, NORMAL)
    assert files == [("lustre/tests/sanity-ec.sh", "the change's diff there has always_except")]


def test_the_file_commented_on_and_files_named_come_first():
    t = th(root_msg="Also sanity-ec needs a case for this, and llite/file.c.")
    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)})
    item = status.build_items(doc, {"overrides": {}, "manual_items": {}})[0][0]
    files = discover.files_for(item, DIFFS, set(), NORMAL)
    assert files[0] == ("lustre/mdd/mdd_object.c", "commented on")
    assert ("lustre/tests/sanity-ec.sh", "named in the thread") in files
    assert ("lustre/llite/file.c", "named in the thread") in files


def test_a_deeper_check_fills_up_with_the_biggest_parts_of_the_change():
    item = aio_item()
    files = dict(discover.files_for(item, DIFFS, set(), DEEP))
    assert set(files) == set(DIFFS)
    assert files["lustre/lov/lov_io.c"] == "changed by this change"


def test_tickets_left_in_the_code_are_found_and_matched_to_the_promise():
    code = discover.code_tickets(DIFFS, own="LU-12669", prefix="LU")
    assert set(code) == {"LU-19631", "LU-20566"}, "the change's own ticket is not a pointer"
    item = aio_item()
    idents = discover.identifiers(discover.item_text(item))
    got = discover.tickets_for(item, code, idents, ["lustre/tests/sanity-ec.sh"], NORMAL)
    assert set(got) == {"LU-19631", "LU-20566"}, "both lines park tests in always_except"
    assert got["LU-20566"].startswith("LU-20566, left in lustre/tests/sanity-ec.sh: always_except")


def test_a_parked_test_ties_its_ticket_to_the_promise_not_a_mention():
    """ "Fix and un-exclude 41d": the ticket 41d is parked under belongs to
    it. An umbrella ticket the thread quotes does not make every line
    naming it relevant."""
    diffs = {
        "lustre/tests/sanity-ec.sh": "+always_except LU-20708 41d\n+always_except LU-20709 53a",
        "lustre/lov/lov_io.c": "+\t * again -- that's a refcount underflow (LU-12668)",
    }
    t = th(
        root_msg="Tests 41d and 53a are disabled in the same patch.",
        replies=["Acknowledged"],
    )
    t["code_context"] = ">>> 29: always_except LU-12668 41d 53a  # (LU-12668)"
    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)})
    item = status.build_items(doc, {"overrides": {}, "manual_items": {}})[0][0]
    code = discover.code_tickets(diffs, own="LU-12669", prefix="LU")
    idents = discover.identifiers(discover.item_text(item))
    got = discover.tickets_for(item, code, idents, list(diffs), NORMAL)
    assert set(got) == {"LU-20708", "LU-20709"}


def test_a_ticket_someone_names_ties_its_parked_lines_to_the_promise():
    diffs = {"lustre/tests/sanity-ec.sh": "+always_except LU-20566 41j 41k"}
    t = th(root_msg="DIO+AIO recovery is missing.", replies=["Filed LU-20566 for that."])
    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)})
    item = status.build_items(doc, {"overrides": {}, "manual_items": {}})[0][0]
    code = discover.code_tickets(diffs, own="LU-12669", prefix="LU")
    got = discover.tickets_for(item, code, set(), [], NORMAL, "LU")
    assert list(got) == ["LU-20566"]


def test_an_unrelated_ticket_in_the_code_needs_a_deeper_check():
    diffs = {"lustre/llite/file.c": "+\t/* LU-30000: revisit */"}
    code = discover.code_tickets(diffs, own="LU-12669", prefix="LU")
    item = aio_item()
    idents = discover.identifiers(discover.item_text(item))
    files = ["lustre/llite/file.c"]
    assert discover.tickets_for(item, code, idents, files, NORMAL) == {}
    assert set(discover.tickets_for(item, code, idents, files, DEEP)) == {"LU-30000"}


class Recorder:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def search_all(self, query, max_results=500, page_size=100, options=None):
        self.calls.append((query, max_results))
        if self.fail:
            raise RuntimeError("gerrit down")
        return [{"_number": 67878, "status": "NEW", "subject": "x", "created": "2026-08-10"}]


def test_searches_stay_in_the_project_and_branch_and_run_once():
    client = Recorder()
    f = discover.Finder(client, project=PUBLIC, branch="master", number=62757)
    a = f.touching("lustre/tests/sanity-ec.sh", "2026-08-22 08:08:54", 50)
    b = f.touching("lustre/tests/sanity-ec.sh", "2026-08-22 08:08:54", 50)
    assert a == b and len(client.calls) == 1
    query = client.calls[0][0]
    for part in (
        f'project:"{PUBLIC}"',
        'branch:"master"',
        "-change:62757",
        "-is:abandoned",
        'path:"lustre/tests/sanity-ec.sh"',
        "after:2026-08-22",
    ):
        assert part in query
    f.siblings("ec2", ["pt_ecro"], "", 50)
    assert '(topic:"ec2" OR hashtag:"pt_ecro")' in client.calls[-1][0]
    assert f.siblings("", [], "", 50) == []


def test_a_failed_search_finds_nothing_and_does_not_raise():
    f = discover.Finder(Recorder(fail=True), project=PUBLIC, branch="master", number=1)
    assert f.on_ticket("LU-20566", "", 50) == []


def test_quotes_cannot_break_out_of_a_term():
    client = Recorder()
    f = discover.Finder(client, project=PUBLIC, branch="master", number=1)
    f.touching('a" OR owner:"x', "", 5)
    assert 'path:"a OR owner:x"' in client.calls[0][0]
