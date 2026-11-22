"""pytest plugin used only at build time: records which tests read which fixture files.

Reads that happen while a test module is being collected (module-level loading) are
recorded against the module, because they affect every test that imports it.
"""
import builtins
import io
import json
import os

import pytest

OUT = os.environ.get("BLAST_OUT", "/tmp/blast")
ROOT = os.getcwd()
_state = {"kind": None, "key": None}
_tests, _mods = {}, {}
_orig_builtin, _orig_io = builtins.open, io.open


def _note(f):
    try:
        if isinstance(f, int):
            return
        p = os.fspath(f)
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        if "fixtures" not in p:
            return
        rel = os.path.relpath(os.path.abspath(p), ROOT).replace(os.sep, "/")
        if not rel.startswith("tests/fixtures/"):
            return
        kind, key = _state["kind"], _state["key"]
        if kind == "t":
            _tests.setdefault(key, set()).add(rel)
        elif kind == "m":
            _mods.setdefault(key, set()).add(rel)
    except Exception:
        pass


def _wrap(orig):
    def _open(f, *a, **k):
        _note(f)
        return orig(f, *a, **k)

    return _open


def pytest_configure(config):
    builtins.open = _wrap(_orig_builtin)
    io.open = _wrap(_orig_io)


def pytest_collectstart(collector):
    if isinstance(collector, pytest.Module):
        _state.update(kind="m", key=collector.nodeid)


def pytest_collection_finish(session):
    _state.update(kind=None, key=None)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logstart(nodeid, location):
    _state.update(kind="t", key=nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _state.update(kind=None, key=None)


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(OUT, exist_ok=True)
    data = {
        "tests": {k: sorted(v) for k, v in _tests.items()},
        "mods": {k: sorted(v) for k, v in _mods.items()},
    }
    with _orig_builtin(os.path.join(OUT, f"fix-{os.getpid()}.json"), "w") as fh:
        json.dump(data, fh)
