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
_inst_tests, _inst_mods = {}, {}  # test / module -> set of expression classes created while it ran
_sink = None
_patched = False
_base = None
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
        if kind == "t" and _state.get("phase") in ("setup", "teardown"):
            # class/module-scoped setup runs inside the first test's setup, but serves every test
            _mods.setdefault(key.split("::")[0], set()).add(rel)
        elif kind == "t":
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


def _patch_classes():
    """Count every Expression instance by class: a class-body edit (arg_types ...) matters to the tests
    that create that class, which coverage cannot show (the class body runs once, at import)."""
    global _patched, _base
    if _patched:
        return
    _patched = True
    try:
        from sqlglot import expressions as E

        base = getattr(E, "Expression", None) or E.Expr
        owner = next(c for c in base.__mro__ if c is not object and "__init__" in c.__dict__)
        orig = owner.__init__

        def init(self, *a, **k):
            s = _sink
            if s is not None:
                s.add(type(self))
            orig(self, *a, **k)

        init.__wrapped__ = orig
        owner.__init__ = init
        _base = base
    except Exception as e:  # tracking is optional: the selector falls back to its old rule
        print("class tracking unavailable:", e)


def pytest_configure(config):
    builtins.open = _wrap(_orig_builtin)
    io.open = _wrap(_orig_io)


def pytest_collectstart(collector):
    global _sink
    _patch_classes()
    if isinstance(collector, pytest.Module):
        _state.update(kind="m", key=collector.nodeid)
        _sink = _inst_mods.setdefault(collector.nodeid, set())


def pytest_collection_finish(session):
    global _sink
    _sink = None
    _state.update(kind=None, key=None)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logstart(nodeid, location):
    global _sink
    _patch_classes()
    _state.update(kind="t", key=nodeid)
    _sink = _inst_mods.setdefault(nodeid.split("::")[0], set())


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    global _sink
    _state["phase"] = "setup"
    _sink = _inst_mods.setdefault(item.nodeid.split("::")[0], set())


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    global _sink
    _state["phase"] = "call"
    _sink = _inst_tests.setdefault(item.nodeid, set())


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    global _sink
    _state["phase"] = "teardown"
    _sink = _inst_mods.setdefault(item.nodeid.split("::")[0], set())


def pytest_runtest_logfinish(nodeid, location):
    global _sink
    _sink = None
    _state.update(kind=None, key=None)


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(OUT, exist_ok=True)
    data = {
        "tests": {k: sorted(v) for k, v in _tests.items()},
        "mods": {k: sorted(v) for k, v in _mods.items()},
    }
    with _orig_builtin(os.path.join(OUT, f"fix-{os.getpid()}.json"), "w") as fh:
        json.dump(data, fh)


    keys = {}

    def names(classes):
        out = set()
        for c in classes:
            k = keys.get(c)
            if k is None:
                k = keys[c] = tuple(f"{b.__module__}.{b.__qualname__}" for b in c.__mro__
                                    if _base is not None and issubclass(b, _base))
            out.update(k)
        return sorted(out)

    inst = {
        "tests": {k: names(v) for k, v in _inst_tests.items()},
        "mods": {k: names(v) for k, v in _inst_mods.items()},
    }
    with _orig_builtin(os.path.join(OUT, f"inst-{os.getpid()}.json"), "w") as fh:
        json.dump(inst, fh)
