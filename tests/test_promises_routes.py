"""The Gerrit Promises pages: who sees what, who may do what."""

import pytest

from portal.promises import jobs
from tests.promises_helpers import adjudication, change_doc, classification, overrides_doc, th

INTERNAL = "internal/example-project"


@pytest.fixture
def app(app_env, monkeypatch, tmp_path):
    """The standard app with Gerrit Promises switched on and the
    pipelines replaced by recorders."""
    monkeypatch.setenv("PORTAL_PROMISES", "1")
    monkeypatch.setenv("PORTAL_PROMISES_DIR", str(tmp_path / "promises"))
    monkeypatch.setenv("PORTAL_PRIVATE_PREFIX", "/private")
    monkeypatch.setenv("PORTAL_PRIVATE_DIR", app_env["private_dir"])
    from portal.app import create_app
    from portal.auth import _cache

    _cache.clear()
    return create_app(testing=True)


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def stub(name):
        def fn(settings, store, number, progress=None, cancel=None, **kw):
            seen.append((name, number, kw))
            return {}

        return fn

    for name in ("run_add", "run_rescan", "run_classify", "run_judge"):
        monkeypatch.setattr(jobs, name, stub(name))
    return seen


@pytest.fixture
def store(app):
    return app.extensions["promises"]["store"]


def seed(store, doc, ov=None):
    store.mutate_change(doc["change_number"], lambda d: (d.clear(), d.update(doc)))
    if ov:
        store.mutate_overrides(doc["change_number"], lambda d: (d.clear(), d.update(ov)))


def wait_jobs(app):
    import time

    reg = app.extensions["promises"]["registry"]
    for _ in range(100):
        if not any(j["state"] == "running" for j in reg.snapshot().values()):
            return
        time.sleep(0.02)


def promise_doc(number=64620, project="fs/lustre-release", **kw):
    t = th()
    return change_doc(
        number=number,
        project=project,
        threads=[t],
        classifications={t["id"]: classification(t)},
        **kw,
    )


# ---------- switched off / on ----------


def test_the_tab_does_not_exist_unless_switched_on(bare_app):
    client = bare_app.test_client()
    assert client.get("/gerrit_promise/").status_code == 404
    assert b"Gerrit Promises" not in client.get("/gerrit_vis/").data


def test_the_tab_is_in_the_nav_when_on(client):
    assert b'href="/gerrit_promise/"' in client.get("/gerrit_vis/").data


# ---------- who sees what ----------


def test_anyone_sees_public_promises(client, store):
    seed(store, promise_doc())
    body = client.get("/gerrit_promise/").data.decode()
    assert "LU-19548 lfs: update mirror split" in body
    page = client.get("/gerrit_promise/64620").data.decode()
    assert "Enforce the rule on the MDS" in page
    assert "will fix in a follow-up patch" in page


def test_internal_changes_are_a_404_like_missing_ones(client, login, store):
    seed(
        store,
        promise_doc(number=70100, project=INTERNAL, subject="LU-19990 internal: secret subject"),
    )
    for who in (None, "alice"):
        if who:
            login(who)
        hidden, missing = client.get("/gerrit_promise/70100"), client.get("/gerrit_promise/99999")
        assert hidden.status_code == missing.status_code == 404
        assert b"secret subject" not in client.get("/gerrit_promise/").data
        assert client.get("/gerrit_promise/70100/export.md").status_code == 404


def test_insiders_see_internal_changes(client, login_internal, store):
    seed(
        store,
        promise_doc(number=70100, project=INTERNAL, subject="LU-19990 internal: secret subject"),
    )
    login_internal()
    assert client.get("/gerrit_promise/70100").status_code == 200
    assert b"secret subject" in client.get("/gerrit_promise/").data


def test_a_change_not_harvested_yet_is_treated_as_internal(client, store):
    store.mutate_change(1234, lambda d: None)
    assert client.get("/gerrit_promise/1234").status_code == 404


def test_the_job_list_hides_what_the_session_may_not_see(app, client, store):
    seed(store, promise_doc(number=70100, project=INTERNAL))
    app.extensions["promises"]["registry"]._jobs[70100] = {"state": "running", "name": "x"}
    app.extensions["promises"]["registry"]._jobs[5555] = {"state": "running", "name": "track"}
    assert client.get("/gerrit_promise/api/jobs").get_json() == {}


# ---------- who may do what ----------


def test_tracking_needs_a_sign_in(client, calls):
    resp = client.post("/gerrit_promise/track", data={"change": "62757"})
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert calls == []


def test_a_signed_in_user_can_track_a_public_change(app, client, login, calls, monkeypatch):
    monkeypatch.setattr(
        jobs.harvest, "change_project", lambda url, n: {"project": "fs/lustre-release"}
    )
    login("alice")
    resp = client.post(
        "/gerrit_promise/track",
        data={"change": "https://review.whamcloud.com/c/fs/lustre-release/+/62757"},
    )
    wait_jobs(app)
    assert resp.status_code == 303
    assert calls == [("run_add", 62757, {"added_by": "alice"})]


def test_tracking_an_internal_change_without_the_role_looks_like_not_found(
    client, login, calls, monkeypatch
):
    monkeypatch.setattr(jobs.harvest, "change_project", lambda url, n: {"project": INTERNAL})
    login("alice")
    internal = client.post(
        "/gerrit_promise/track", data={"change": "70100"}, follow_redirects=True
    ).data

    def missing(url, n):
        raise RuntimeError("404")

    monkeypatch.setattr(jobs.harvest, "change_project", missing)
    absent = client.post(
        "/gerrit_promise/track", data={"change": "66545"}, follow_redirects=True
    ).data
    assert b"is not available" in internal and b"is not available" in absent
    assert calls == []


def test_a_signed_in_user_can_rescan_but_not_run_claude(app, client, login, store, calls):
    seed(store, promise_doc())
    login("alice")
    assert client.post("/gerrit_promise/64620/rescan").status_code == 303
    for path in (
        "/gerrit_promise/64620/classify",
        "/gerrit_promise/64620/judge",
        "/gerrit_promise/64620/item/a08dac95_b61396c6/judge",
    ):
        assert client.post(path).status_code == 403, path
    wait_jobs(app)
    assert [c[0] for c in calls] == ["run_rescan"]


def test_the_admin_runs_claude(app, client, login_admin, store, calls):
    seed(store, promise_doc())
    login_admin()
    client.post("/gerrit_promise/64620/classify")
    wait_jobs(app)
    client.post("/gerrit_promise/64620/judge", data={"scope": "open"})
    wait_jobs(app)
    assert [(c[0], c[2].get("scope")) for c in calls] == [
        ("run_classify", None),
        ("run_judge", "open"),
    ]


def test_only_the_admin_decides_an_item(client, login, login_admin, store):
    seed(store, promise_doc())
    tid = "a08dac95_b61396c6"
    login("alice")
    assert (
        client.post(
            f"/gerrit_promise/64620/item/{tid}/override", data={"status": "done"}
        ).status_code
        == 403
    )
    login_admin()
    client.post(
        f"/gerrit_promise/64620/item/{tid}/override",
        data={"status": "done", "note": "landed in 70001"},
    )
    ov = store.load_overrides(64620)["overrides"][tid]
    assert (ov["status"], ov["note"], ov["set_by"]) == ("done", "landed in 70001", "root_user")


def test_a_public_change_cannot_link_an_internal_follow_up(client, login_admin, store, monkeypatch):
    """The page is public; the link would publish the other change."""
    seed(store, promise_doc())
    monkeypatch.setattr(jobs.Settings, "gerrit", lambda self: object())
    monkeypatch.setattr(
        "portal.promises.followups.fetch_linked_changes",
        lambda c, n: {str(n[0]): {"project": INTERNAL, "subject": "secret", "status": "NEW"}},
    )
    login_admin()
    body = client.post(
        "/gerrit_promise/64620/item/a08dac95_b61396c6/override",
        data={"followup_change": "70001"},
        follow_redirects=True,
    ).data
    assert b"can only link follow-ups in fs/lustre-release" in body
    assert store.load_overrides(64620)["overrides"] == {}


def test_a_typo_in_the_link_never_clears_it(client, login_admin, store):
    seed(store, promise_doc(), overrides_doc({"a08dac95_b61396c6": {"followup_change": 70001}}))
    login_admin()
    client.post(
        "/gerrit_promise/64620/item/a08dac95_b61396c6/override", data={"followup_change": "oops"}
    )
    assert store.load_overrides(64620)["overrides"]["a08dac95_b61396c6"]["followup_change"] == 70001


def test_untracking_is_admin_only_and_keeps_the_data(client, login, login_admin, store):
    seed(store, promise_doc())
    login("alice")
    assert client.post("/gerrit_promise/64620/untrack").status_code == 403
    login_admin()
    client.post("/gerrit_promise/64620/untrack")
    assert not store.has_change(64620) and store.unarchive_change(64620)


def test_posts_need_a_csrf_token(client, login_admin, store):
    seed(store, promise_doc())
    login_admin()
    assert client.post("/gerrit_promise/64620/classify", csrf=False).status_code == 400


# ---------- what the pages show ----------


def test_claude_buttons_show_only_to_the_admin(client, login, login_admin, store, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    seed(store, promise_doc())
    assert b"Find promises" not in client.get("/gerrit_promise/64620").data
    login("alice")
    page = client.get("/gerrit_promise/64620").data
    assert b"Rescan" in page and b"Find promises" not in page and b"Your call" not in page
    login_admin()
    assert b"Your call" in client.get("/gerrit_promise/64620").data


def test_checking_is_explained_where_unsupported(client, login_admin, store, monkeypatch):
    monkeypatch.setattr("portal.promises.claude.find_binary", lambda configured=None: "/bin/true")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    seed(store, promise_doc(number=70100, project=INTERNAL, branch="b2_15"))
    login_admin()
    assert b"only supported for" in client.get("/gerrit_promise/70100").data


def test_a_verdict_is_shown_with_its_evidence(client, store):
    t = th()
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={t["id"]: adjudication(t)},
    )
    seed(store, doc)
    page = client.get("/gerrit_promise/64620").data.decode()
    assert "still present at the pinned revision" in page and "AI check" in page


def test_the_open_view_lists_open_promises_across_changes(client, store):
    t = th()
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={t["id"]: adjudication(t)},
    )
    seed(store, doc)
    body = client.get("/gerrit_promise/?view=open").data.decode()
    assert "Enforce the rule on the MDS" in body and "Marc Vef" in body


def test_export_is_markdown_with_who_promised(client, store):
    seed(store, promise_doc())
    resp = client.get("/gerrit_promise/64620/export.md")
    assert resp.mimetype == "text/markdown"
    assert "promise by Marc Vef" in resp.get_data(as_text=True)


def test_the_admin_can_tick_a_promise_off_as_not_needed(client, login_admin, store):
    seed(store, promise_doc())
    login_admin()
    client.post(
        "/gerrit_promise/64620/item/a08dac95_b61396c6/override", data={"status": "not_needed"}
    )
    assert store.load_overrides(64620)["overrides"]["a08dac95_b61396c6"]["status"] == "not_needed"
    page = client.get("/gerrit_promise/64620").data.decode()
    assert "decided: not needed" in page


def test_a_found_follow_up_can_be_linked_in_one_click(client, login_admin, store, monkeypatch):
    t = th(replies=["I will create a patch in LU-19999."])
    found = {
        "by_ticket": {
            "LU-19999": [{"number": 70001, "subject": "LU-19999 mdt: fix", "status": "NEW"}]
        }
    }
    seed(
        store,
        change_doc(
            threads=[t],
            classifications={
                t["id"]: classification(t, quote="I will create a patch in LU-19999.")
            },
            candidates=found,
        ),
    )
    monkeypatch.setattr(jobs.Settings, "gerrit", lambda self: object())
    monkeypatch.setattr(
        "portal.promises.followups.fetch_linked_changes",
        lambda c, n: {
            str(n[0]): {
                "project": "fs/lustre-release",
                "status": "NEW",
                "subject": "LU-19999 mdt: fix",
            }
        },
    )
    login_admin()
    assert b">Link</button>" in client.get("/gerrit_promise/64620").data
    client.post(
        f"/gerrit_promise/64620/item/{t['id']}/override",
        data={"followup_change": "70001", "action": "save"},
    )
    page = client.get("/gerrit_promise/64620").data.decode()
    assert "In flight" in page and "in flight: 70001" in page


def test_promise_pages_are_never_cached(client, store):
    seed(store, promise_doc())
    for path in ("/gerrit_promise/", "/gerrit_promise/64620", "/gerrit_promise/api/jobs"):
        assert "no-store" in client.get(path).headers.get("Cache-Control", ""), path


def test_tracking_says_it_started(app, client, login, calls, monkeypatch):
    monkeypatch.setattr(
        jobs.harvest, "change_project", lambda url, n: {"project": "fs/lustre-release"}
    )
    login("alice")
    body = client.post(
        "/gerrit_promise/track", data={"change": "62757"}, follow_redirects=True
    ).data
    wait_jobs(app)
    assert b"Tracking 62757" in body


def test_unread_threads_are_not_called_judged(client, store):
    seed(store, change_doc(threads=[th(keyword_hit=False)]))
    page = client.get("/gerrit_promise/64620").data.decode()
    assert "Claude has not read yet" in page and "judged not a promise" not in page.lower()


def test_the_per_item_button_says_check_until_there_is_a_verdict(
    client, login_admin, store, monkeypatch
):
    monkeypatch.setattr("portal.promises.claude.find_binary", lambda configured=None: "/bin/true")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    t = th()
    seed(store, change_doc(threads=[t], classifications={t["id"]: classification(t)}))
    login_admin()
    assert b">Check</button>" in client.get("/gerrit_promise/64620").data
    store.mutate_change(64620, lambda d: d["adjudications"].update({t["id"]: adjudication(t)}))
    assert b">Check again</button>" in client.get("/gerrit_promise/64620").data


def test_only_the_admin_cancels(app, client, login, login_admin, store):
    import threading

    seed(store, promise_doc())
    reg = app.extensions["promises"]["registry"]
    stop = threading.Event()
    reg.start(64620, "check promises", lambda progress, cancel: (cancel.wait(5), stop.set()))
    login("alice")
    assert client.post("/gerrit_promise/64620/cancel").status_code == 403
    login_admin()
    assert client.post("/gerrit_promise/64620/cancel").status_code == 303
    assert stop.wait(2)
    wait_jobs(app)
    assert reg.snapshot()["64620"]["state"] == "cancelled"


def test_duplicates_show_inside_their_primary_and_can_be_split_or_merged(
    client, login_admin, store
):
    a, b, c = th(tid="aa_1", ps=5), th(tid="bb_2", ps=9), th(tid="cc_3", ps=7)
    doc = change_doc(
        threads=[a, b, c],
        classifications={t["id"]: classification(t, summary=f"do {t['id']}") for t in (a, b, c)},
    )
    doc["groups"] = {
        "prompt_version": "g1",
        "groups": [{"ids": ["aa_1", "bb_2"], "reason": "same thing"}],
    }
    seed(store, doc)
    page = client.get("/gerrit_promise/64620").data.decode()
    assert "Also raised in 1 other thread" in page and 'id="item-aa_1"' in page
    assert "1 duplicate folded in" in page
    login_admin()
    client.post("/gerrit_promise/64620/item/aa_1/override", data={"action": "not_duplicate"})
    assert store.load_overrides(64620)["overrides"]["aa_1"]["not_duplicate"] is True
    assert "Also raised in" not in client.get("/gerrit_promise/64620").data.decode()
    client.post(
        "/gerrit_promise/64620/item/cc_3/override",
        data={"duplicate_of": "64620-bb", "action": "save"},
    )
    assert store.load_overrides(64620)["overrides"]["cc_3"]["duplicate_of"] == "bb_2"
    body = client.post(
        "/gerrit_promise/64620/item/cc_3/override",
        data={"duplicate_of": "64620-zzz", "action": "save"},
        follow_redirects=True,
    ).data
    assert b"is not another promise of this change" in body


def test_an_unverifiable_follow_up_is_refused_on_a_public_change(
    client, login_admin, store, monkeypatch
):
    """If Gerrit does not answer, the project is unknown -- and a later
    rescan would publish the linked change, whatever it is."""
    seed(store, promise_doc())
    monkeypatch.setattr(jobs.Settings, "gerrit", lambda self: object())
    monkeypatch.setattr("portal.promises.followups.fetch_linked_changes", lambda c, n: {})
    login_admin()
    body = client.post(
        "/gerrit_promise/64620/item/a08dac95_b61396c6/override",
        data={"followup_change": "70001"},
        follow_redirects=True,
    ).data
    assert b"could not look up change 70001" in body
    assert store.load_overrides(64620)["overrides"] == {}


def test_the_page_says_when_the_check_ruled_the_follow_ups_out(client, store):
    t = th(replies=["I will create a patch in LU-19999."])
    found = {
        "by_ticket": {
            "LU-19999": [
                {
                    "number": 70001,
                    "status": "NEW",
                    "subject": "LU-19999 x",
                    "created": "2026-09-02 10:00:00",
                }
            ]
        }
    }
    doc = change_doc(threads=[t], classifications={t["id"]: classification(t)}, candidates=found)
    seed(store, doc)
    page = client.get("/gerrit_promise/64620").data.decode()
    assert 'not confirmed">1 possible follow-up' in page and "Possible follow-ups" in page
    store.mutate_change(
        64620, lambda d: d["adjudications"].update({t["id"]: adjudication(t, verdict="still-open")})
    )
    page = client.get("/gerrit_promise/64620").data.decode()
    assert "none keeps the promise" in page
    assert 'not confirmed">1 possible follow-up' not in page, "no chip once ruled out"
    store.mutate_change(
        64620,
        lambda d: d["adjudications"].update(
            {
                t["id"]: adjudication(
                    t, verdict="addressed", addressed_in={"kind": "change", "ref": "70001"}
                )
            }
        ),
    )
    page = client.get("/gerrit_promise/64620").data.decode()
    assert "kept in 70001" in page and "the check: kept here" in page


def test_a_promise_in_a_follow_up_has_its_own_section_and_says_which(client, store):
    t = th()
    doc = change_doc(
        threads=[t],
        classifications={t["id"]: classification(t)},
        adjudications={
            t["id"]: adjudication(
                t, verdict="in-followup", addressed_in={"kind": "change", "ref": "67878"}
            )
        },
        candidates={
            "stacked": [
                {"number": 67878, "status": "NEW", "subject": "LU-20566 ec: recover O_DIRECT reads"}
            ]
        },
    )
    seed(store, doc)
    page = client.get("/gerrit_promise/64620").data.decode()
    assert '>In flight <span class="count">1</span>' in page
    assert "in flight: 67878" in page and "In flight:</b>" in page
    assert "1 in flight" in client.get("/gerrit_promise/").data.decode()


def test_the_nav_order_and_the_alpha_tag(client):
    """Dashboard, Promises, Visualizer, then the private area."""
    body = client.get("/gerrit_promise/").data.decode()
    assert body.index(">Gerrit Promises</a>") < body.index(">Gerrit Visualizer</a>")
    assert '<span class="beta-tag">alpha</span>' in body
    assert "beta-tag" not in client.get("/gerrit_vis/").data.decode(), "the Visualizer is no longer a beta"
