"""Configuration edge cases that bit a real deployment."""

import os
import stat
import sys


def test_gc_bin_is_found_beside_the_interpreter(tmp_path, monkeypatch):
    """Under systemd, PATH does not include the venv's bin/. The CLI is
    installed right next to the interpreter, so look there first."""
    from portal import config

    fake_prefix = tmp_path / "venv"
    (fake_prefix / "bin").mkdir(parents=True)
    gc = fake_prefix / "bin" / "gc"
    gc.write_text("#!/bin/sh\n")
    gc.chmod(gc.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setattr(sys, "prefix", str(fake_prefix))
    monkeypatch.delenv("PORTAL_GC_BIN", raising=False)
    monkeypatch.delenv("GC_BIN", raising=False)
    # Make PATH useless, to prove the venv lookup is what found it.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("PORTAL_SECRET_KEY", "x")

    assert config.build_config()["GC_BIN"] == str(gc)


def test_gc_bin_falls_back_to_path(tmp_path, monkeypatch):
    from portal import config

    on_path = tmp_path / "bin"
    on_path.mkdir()
    gc = on_path / "gc"
    gc.write_text("#!/bin/sh\n")
    gc.chmod(gc.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setattr(sys, "prefix", str(tmp_path / "no-venv-here"))
    monkeypatch.delenv("PORTAL_GC_BIN", raising=False)
    monkeypatch.delenv("GC_BIN", raising=False)
    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.setenv("PORTAL_SECRET_KEY", "x")

    assert config.build_config()["GC_BIN"] == str(gc)


def test_gc_bin_explicit_override_wins(monkeypatch):
    from portal import config

    monkeypatch.setenv("PORTAL_GC_BIN", "/somewhere/specific/gc")
    monkeypatch.setenv("PORTAL_SECRET_KEY", "x")
    assert config.build_config()["GC_BIN"] == "/somewhere/specific/gc"


def test_missing_gc_still_yields_a_name_for_the_error(tmp_path, monkeypatch):
    """When nothing is found, keep the bare name so the eventual
    FileNotFoundError says 'gc' rather than something cryptic."""
    from portal import config

    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nope"))
    monkeypatch.delenv("PORTAL_GC_BIN", raising=False)
    monkeypatch.delenv("GC_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("PORTAL_SECRET_KEY", "x")
    assert config.build_config()["GC_BIN"] == "gc"


def test_data_dir_defaults_relative_to_cwd(tmp_path, monkeypatch):
    """A fresh checkout runs against ./data with nothing configured."""
    from portal import config

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PORTAL_DATA_DIR", raising=False)
    monkeypatch.setenv("PORTAL_SECRET_KEY", "x")
    cfg = config.build_config()
    assert cfg["DATA_DIR"] == str(tmp_path / "data")
    assert cfg["GRAPH_OUTPUT_DIR"] == os.path.join(str(tmp_path / "data"), "graphs")
