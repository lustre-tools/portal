"""Patch Status on the site: the job's files and lock, the page, its
status and Update endpoints, and the TLC layout it is the first user of."""

import gzip
import json
import os
import time

import pytest

from portal.patch_status import job
from portal.patch_status.fetch import FetchError


@pytest.fixture
def ps_dir(tmp_path):
    return str(tmp_path / "patch_status")


@pytest.fixture
def ps_app(app_env, ps_dir, monkeypatch):
    monkeypatch.setenv("PORTAL_PATCH_STATUS", "1")
    monkeypatch.setenv("PORTAL_PATCH_STATUS_DIR", ps_dir)
    from portal.app import create_app
    from portal.auth import _cache
    from tests.conftest import CSRFClient

    _cache.clear()
    app = create_app(testing=True)
    app.test_client_class = CSRFClient
    return app


@pytest.fixture
def ps_client(ps_app):
    return ps_app.test_client()


def publish(directory, body='<div class="ps"><p>the body</p></div>', fetched_at=1795623639.0):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, job.PAGE), "w") as f:
        f.write(body)
    with open(os.path.join(directory, job.META), "w") as f:
        json.dump({"version": f"{fetched_at:.0f}", "fetched_at": fetched_at, "project": "fs/lustre-release",
                   "branch": "master", "total": 1, "seconds": 120}, f)


def write_status(directory, **fields):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, job.STATUS), "w") as f:
        json.dump(fields, f)


# ---------- job ----------

def fake_fetch(**kw):
    kw["progress"]("changes", 1, None)
    return {
        "branch": "master", "project": "fs/lustre-release", "head": "b" * 40, "fetched_at": 1000.0,
        "changes": [{
            "_number": 5, "subject": "LU-5 mdt: x", "project": "fs/lustre-release", "branch": "master",
            "owner": {"_account_id": 1, "name": "O"}, "labels": {}, "messages": [],
            "current_revision": "r", "revisions": {"r": {"_number": 1}}, "mergeable": True,
            "hashtags": [], "_related": [], "_jira_ticket": "LU-5", "_jira": {},
        }],
    }


RUN = dict(gerrit_url="https://g", jira_url="https://j", project="fs/lustre-release", branch="master")


def test_a_run_writes_the_page_meta_and_status(ps_dir, monkeypatch):
    monkeypatch.setattr("portal.patch_status.fetch.fetch", fake_fetch)
    meta = job.run(ps_dir, **RUN)
    assert meta["total"] == 1 and meta["version"] == "1000"
    assert job.read_meta(ps_dir)["head"] == "b" * 40
    assert "LU-5" in job.read_page(ps_dir)
    st = job.read_status(ps_dir)
    assert st["state"] == "done" and not st["running"] and st["started_at"]


def test_the_page_says_how_rebases_were_checked(ps_dir, monkeypatch):
    from portal.patch_status.trial import TrialError

    monkeypatch.setattr("portal.patch_status.fetch.fetch", fake_fetch)
    monkeypatch.setattr("portal.patch_status.trial.try_picks", lambda *a, **k: 7)
    assert job.run(ps_dir, **RUN)["rebase_check"] == "trial picks onto master (7 tried)"
    assert "needs-rebase check: trial picks onto master (7 tried)" in job.read_page(ps_dir)

    def broken(*a, **k):
        raise TrialError("no git")

    monkeypatch.setattr("portal.patch_status.trial.try_picks", broken)
    assert job.run(ps_dir, **RUN)["rebase_check"] == "wc-checkpatch votes (the trial picks failed)"
    assert job.run(ps_dir, trial_picks=False, **RUN)["rebase_check"] == "wc-checkpatch votes"


def test_a_failed_run_keeps_the_last_page_and_says_why(ps_dir, monkeypatch):
    publish(ps_dir, body="previous")

    def fail(**kw):
        raise FetchError("Gerrit is down")

    monkeypatch.setattr("portal.patch_status.fetch.fetch", fail)
    with pytest.raises(FetchError):
        job.run(ps_dir, **RUN)
    assert job.read_page(ps_dir) == "previous"
    st = job.read_status(ps_dir)
    assert st["state"] == "failed" and st["error"] == "Gerrit is down"


def test_an_unexpected_error_is_not_shown_on_the_page(ps_dir, monkeypatch):
    def boom(**kw):
        raise OSError(2, "No such file or directory", "/etc/secret/place.json")

    monkeypatch.setattr("portal.patch_status.fetch.fetch", boom)
    with pytest.raises(OSError):
        job.run(ps_dir, **RUN)
    st = job.read_status(ps_dir)
    assert st["state"] == "failed" and "/etc" not in st["error"]


def test_one_run_at_a_time(ps_dir, monkeypatch):
    seen = {}

    def nested(**kw):
        seen["running"] = job.is_running(ps_dir)
        with pytest.raises(job.Busy):
            job.run(ps_dir, **RUN)
        return fake_fetch(**kw)

    monkeypatch.setattr("portal.patch_status.fetch.fetch", nested)
    job.run(ps_dir, **RUN)
    assert seen["running"] and not job.is_running(ps_dir)


def test_a_run_that_died_does_not_count_as_running(ps_dir):
    write_status(ps_dir, state="running", phase="series", started_at=time.time())
    st = job.read_status(ps_dir)
    assert not st["running"] and st["state"] == "failed"


def test_the_floor_counts_from_the_last_start(ps_dir):
    assert job.next_allowed(ps_dir, 900) is None
    write_status(ps_dir, state="failed", started_at=1000.0)
    assert job.next_allowed(ps_dir, 900, now=1500.0) == 1900.0
    assert job.next_allowed(ps_dir, 900, now=2000.0) is None


def test_the_cli_reports_a_failed_fetch(ps_dir, app_env, monkeypatch, capsys):
    monkeypatch.setenv("PORTAL_PATCH_STATUS_DIR", ps_dir)
    monkeypatch.setattr("portal.patch_status.fetch.fetch",
                        lambda **kw: (_ for _ in ()).throw(FetchError("down")))
    assert job.main(["--quiet"]) == 9
    assert "the page is unchanged" in capsys.readouterr().err


# ---------- the page ----------

def test_off_unless_configured(client):
    assert client.get("/patch_status/").status_code == 404
    assert "Patch Status" not in client.get("/gerrit_vis/").get_data(as_text=True)


def test_the_tab_and_the_page_before_the_first_run(ps_client):
    html = ps_client.get("/gerrit_vis/").get_data(as_text=True)
    assert 'href="/patch_status/"' in html
    r = ps_client.get("/patch_status/")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "There is no Patch Status page yet" in html
    assert 'class="nav-link nav-active">Patch Status' in html.replace("  ", " ")
    assert "no-store" in r.headers["Cache-Control"]


def test_the_page_shows_the_last_body(ps_client, ps_dir):
    publish(ps_dir)
    html = ps_client.get("/patch_status/").get_data(as_text=True)
    assert '<p>the body</p>' in html
    assert 'data-job-version="1795623639"' in html
    assert "updated 2026-11-25" in html


def test_tlc_by_default_classic_with_the_dashboards_cookie(ps_client, ps_dir):
    publish(ps_dir)
    html = ps_client.get("/patch_status/").get_data(as_text=True)
    assert "patch_status/tlc.css" in html and "tlc-page.css" in html
    assert 'var DESIGN = "tlc"' in html
    ps_client.set_cookie("gd-design", "classic")
    html = ps_client.get("/patch_status/").get_data(as_text=True)
    assert "patch_status/classic.css" in html and "tlc-classic.css" in html
    assert 'var DESIGN = "classic"' in html


def test_a_large_page_is_sent_compressed(ps_client, ps_dir):
    publish(ps_dir, body='<div class="ps">' + "<p>row</p>" * 2000 + "</div>")
    r = ps_client.get("/patch_status/", headers={"Accept-Encoding": "gzip, br"})
    assert r.headers["Content-Encoding"] == "gzip"
    assert "<p>row</p>" in gzip.decompress(r.get_data()).decode()
    assert "Content-Encoding" not in ps_client.get("/patch_status/").headers


def test_a_failed_update_is_shown_above_the_last_good_page(ps_client, ps_dir):
    publish(ps_dir)
    write_status(ps_dir, state="failed", error="Gerrit is down", started_at=1.0)
    html = ps_client.get("/patch_status/").get_data(as_text=True)
    assert "The last update failed: Gerrit is down" in html and "<p>the body</p>" in html


# ---------- status and Update ----------

class FakeProc:
    started = []

    def __init__(self, args, **kw):
        FakeProc.started.append(args)
        self.pid = 4242
        self.done = False

    def poll(self):
        return 0 if self.done else None

    def wait(self):
        return 0


@pytest.fixture
def fake_popen(monkeypatch):
    from portal.blueprints import patch_status as bp

    FakeProc.started = []
    monkeypatch.setattr(bp.subprocess, "Popen", FakeProc)
    monkeypatch.setitem(bp._child, "proc", None)
    return FakeProc


def test_status_reports_the_page_and_the_floor(ps_client, ps_dir):
    publish(ps_dir)
    write_status(ps_dir, state="done", started_at=time.time() - 60)
    s = ps_client.get("/patch_status/api/status").get_json()
    assert s["running"] is False and s["version"] == "1795623639"
    assert s["next_update_at"] and s["next_update_at"] > time.time()
    assert s["error"] is None


def test_update_starts_one_run_and_no_second(ps_client, fake_popen):
    r = ps_client.post("/patch_status/api/update").get_json()
    assert r["started"] is True
    assert fake_popen.started[0][1:] == ["-m", "portal.patch_status.job", "--quiet"]
    # The child has not taken the lock yet; this process still knows.
    r = ps_client.post("/patch_status/api/update").get_json()
    assert r["started"] is False and "already running" in r["message"]
    assert len(fake_popen.started) == 1
    assert ps_client.get("/patch_status/api/status").get_json()["running"] is True


def test_update_waits_for_the_floor(ps_client, ps_dir, fake_popen):
    write_status(ps_dir, state="done", started_at=time.time() - 60)
    r = ps_client.post("/patch_status/api/update").get_json()
    assert r["started"] is False and "next update can start at" in r["message"]
    assert fake_popen.started == []


def test_update_needs_the_csrf_token(ps_client, fake_popen):
    assert ps_client.post("/patch_status/api/update", csrf=False).status_code == 400
    assert fake_popen.started == []


# ---------- the shared navbar ----------

def test_every_layout_marks_its_own_tab(client):
    html = client.get("/gerrit_vis/").get_data(as_text=True)
    assert 'nav-link nav-active">Gerrit Visualizer' in " ".join(html.split())
    assert "nav.css" in html
