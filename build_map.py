#!/usr/bin/env python3
"""Build-time: fetch sqlglot at the pinned commit, run its tests under per-test coverage,
and write /app/map.json.gz (test <-> function / file / fixture / dialect-name maps).

Only uses the pinned commit's own code and tests; nothing is looked up by patch.
"""
import ast
import glob
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
SRC = os.environ.get("SRC_DIR", "/opt/sqlglot")
OUT = os.environ.get("MAP_OUT", "/app/map.json.gz")
COV = "/tmp/cov/.coverage"
BLAST = "/tmp/blast"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch():
    if os.path.isdir(os.path.join(SRC, "sqlglot")):
        return
    url = f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz"
    log("downloading", url)
    with urllib.request.urlopen(url, timeout=180) as r:
        data = r.read()
    tmp = SRC + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        t.extractall(tmp, filter="data")
    os.rename(os.path.join(tmp, os.listdir(tmp)[0]), SRC)
    shutil.rmtree(tmp, ignore_errors=True)


def env():
    return dict(
        os.environ,
        PYTHONPATH=f"{SRC}:/app",
        SKIP_INTEGRATION="0",
        COVERAGE_FILE=COV,
        BLAST_OUT=BLAST,
        PYTHONDONTWRITEBYTECODE="1",
    )


def collect_ids():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=SRC, env=env(), capture_output=True, text=True,
    )
    ids = [l.strip() for l in r.stdout.splitlines() if "::" in l and not l.startswith(" ")]
    log("collected", len(ids))
    return ids


def run_tests():
    shutil.rmtree(os.path.dirname(COV), ignore_errors=True)
    shutil.rmtree(BLAST, ignore_errors=True)
    os.makedirs(os.path.dirname(COV))
    cmd = [
        sys.executable, "-m", "pytest", "tests", "-n", "4", "-q", "-p", "fixture_tracker",
        "-p", "no:cacheprovider", "--cov=sqlglot", "--cov=tests", "--cov-context=test",
        "--cov-report=", "--timeout=300",
    ]
    r = subprocess.run(cmd, cwd=SRC, env=env())
    log("pytest exit", r.returncode)


def units_of(src):
    us = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ds = min([n.lineno] + [d.lineno for d in n.decorator_list])
            us.append([ds, n.body[0].lineno, n.end_lineno])
    return us


GROUP = "/tmp/grp"
POOL_FILES = ("tests/test_optimizer.py", "tests/test_executor.py")
POOL_MARKERS = ("check_file", "pool", "parse_and_optimize", "ProcessPoolExecutor", "get_context")


def pool_tests(ids_list, src_root=None):
    """Tests that hand their work to a spawned process pool.

    Per-test coverage cannot see inside those workers, and the pool is shared, so the
    workers' lines cannot be tied to one test. They are credited to this whole group.
    """
    root = src_root or SRC
    out = set()
    for rel in POOL_FILES:
        src = open(os.path.join(root, rel)).read()
        for cls in [n for n in ast.parse(src).body if isinstance(n, ast.ClassDef)]:
            segs = {}
            for m in cls.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    segs[m.name] = ast.get_source_segment(src, m) or ""
            marked = {n for n, s in segs.items() if any(k in s for k in POOL_MARKERS)}
            changed = True
            while changed:
                changed = False
                for n, s in segs.items():
                    if n not in marked and any(f"self.{h}" in s for h in marked):
                        marked.add(n)
                        changed = True
            out |= {f"{rel}::{cls.name}::{n}" for n in marked if n.startswith("test")}
    return sorted(i for i in ids_list if i.split("[")[0] in out)


def run_group(ids):
    """Run only the pool-using tests with coverage that follows spawned worker processes."""
    import coverage

    shutil.rmtree(GROUP, ignore_errors=True)
    os.makedirs(GROUP)
    rc = os.path.join(GROUP, "rc")
    with open(rc, "w") as fh:
        fh.write("[run]\nparallel = True\npatch = subprocess\nsource =\n    sqlglot\n    tests\n")
    e = env()
    e["COVERAGE_FILE"] = os.path.join(GROUP, ".coverage")
    cmd = [sys.executable, "-m", "coverage", "run", "--rcfile", rc, "-m", "pytest", "-n", "4", "-q",
           "-p", "no:cacheprovider", "--timeout=300", *ids]
    r = subprocess.run(cmd, cwd=SRC, env=e)
    log("group pytest exit", r.returncode, "tests", len(ids))
    r = subprocess.run([sys.executable, "-m", "coverage", "combine", "--rcfile", rc, GROUP], cwd=SRC, env=e)
    log("group combine exit", r.returncode)
    cd = coverage.CoverageData(basename=e["COVERAGE_FILE"])
    cd.read()
    out = {}
    for f in cd.measured_files():
        rel = os.path.relpath(f, SRC).replace(os.sep, "/")
        out[rel] = set(cd.lines(f) or [])
    # Sanity check: a function body that only the pool workers run in these tests must show up.
    chk = "sqlglot/optimizer/eliminate_joins.py"
    fn = next(n for n in ast.walk(ast.parse(open(os.path.join(SRC, chk)).read()))
              if isinstance(n, ast.FunctionDef) and n.name == "_should_eliminate_join")
    ran = [ln for ln in out.get(chk, ()) if fn.body[0].lineno <= ln <= fn.end_lineno]
    log("group files", len(out), "| worker-only lines seen in _should_eliminate_join:", len(ran))
    if not ran:
        raise SystemExit("worker processes were not measured: failing the build rather than ship a bad map")
    return out


def read_coverage(index, group_lines, group_ids):
    import coverage

    cd = coverage.CoverageData(basename=COV)
    cd.read()
    units, filehit = {}, {}
    gset = {index[i] for i in group_ids}
    todo = {}
    for f in cd.measured_files():
        todo[os.path.relpath(f, SRC).replace(os.sep, "/")] = f
    for rel in group_lines:
        todo.setdefault(rel, os.path.join(SRC, rel))
    for rel, f in todo.items():
        if rel.startswith("..") or not rel.endswith(".py"):
            continue
        base = os.path.basename(rel)
        if not (rel.startswith("sqlglot/") or (rel.startswith("tests/") and not base.startswith("test_"))):
            continue
        try:
            us = units_of(open(os.path.join(SRC, rel)).read())
        except Exception as e:
            log("skip", rel, e)
            continue
        nlines = max([u[2] for u in us] + [0]) + 2
        owner = [-1] * (nlines + 1)
        order = sorted(range(len(us)), key=lambda i: -(us[i][2] - us[i][1]))
        for i in order:
            for ln in range(us[i][1], us[i][2] + 1):
                owner[ln] = i
        hits = [set() for _ in us]
        fh = set()
        glines = group_lines.get(rel, set())
        if glines:
            fh |= gset
            for ln in glines:
                if ln < len(owner) and owner[ln] != -1:
                    hits[owner[ln]] |= gset
        try:
            by_line = cd.contexts_by_lineno(f)
        except Exception:
            by_line = {}
        for ln, ctxs in by_line.items():
            ids = set()
            for c in ctxs:
                nid = c.rsplit("|", 1)[0]
                if not nid:
                    continue
                if nid not in index:
                    index[nid] = len(index)
                ids.add(index[nid])
            fh |= ids
            if ln < len(owner) and owner[ln] != -1:
                hits[owner[ln]] |= ids
        units[rel] = [[u[0], u[1], u[2], sorted(h)] for u, h in zip(us, hits)]
        filehit[rel] = sorted(fh)
    return units, filehit


def dialect_names():
    d = os.path.join(SRC, "sqlglot", "dialects")
    return sorted(
        os.path.basename(p)[:-3]
        for p in glob.glob(os.path.join(d, "*.py"))
        if os.path.basename(p) not in ("__init__.py", "dialect.py")
    )


def scan_mentions(names, ids):
    """Static scan: which dialect names does each test (function + its class) mention?"""
    rx = re.compile(r"\b(%s)\b" % "|".join(sorted(names, key=len, reverse=True)))
    per = {}
    allmark = ("Dialect.classes", "Dialects", "DIALECTS")

    def consts(node):
        found = set()
        for c in ast.walk(node):
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                found.update(rx.findall(c.value.lower()))
        return found

    for path in glob.glob(os.path.join(SRC, "tests", "**", "test_*.py"), recursive=True):
        rel = os.path.relpath(path, SRC).replace(os.sep, "/")
        src = open(path).read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                s = consts(node)
                if any(m in (ast.get_source_segment(src, node) or "") for m in allmark):
                    s = set(names)
                per[f"{rel}::{node.name}"] = s
            elif isinstance(node, ast.ClassDef):
                cls = set()
                for sub in node.body:
                    if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        cls |= consts(sub)
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        s = consts(sub) | cls
                        if any(m in (ast.get_source_segment(src, sub) or "") for m in allmark):
                            s = set(names)
                        per[f"{rel}::{node.name}::{sub.name}"] = s
    out = {n: set() for n in names}
    for nid, i in ids.items():
        base = nid.split("[")[0]
        s = set(per.get(base, ()))
        m = re.match(r"tests/(?:dialects/)?test_([a-z0-9_]+)\.py", nid)
        if m and m.group(1) in out:
            s.add(m.group(1))
        for n in s:
            out[n].add(i)
    return out


def main():
    fetch()
    ids_list = collect_ids()
    run_tests()
    index = {nid: i for i, nid in enumerate(ids_list)}
    group_ids = pool_tests(ids_list)
    log("pool-using tests:", len(group_ids))
    group_lines = run_group(group_ids)
    units, filehit = read_coverage(index, group_lines, group_ids)
    names = dialect_names()
    mentions = scan_mentions(names, index)

    # fixtures
    tests_fix, mods_fix = {}, {}
    for p in glob.glob(os.path.join(BLAST, "fix-*.json")):
        d = json.load(open(p))
        for k, v in d["tests"].items():
            tests_fix.setdefault(k, set()).update(v)
        for k, v in d["mods"].items():
            mods_fix.setdefault(k, set()).update(v)
    helper_mods = set()
    for path in glob.glob(os.path.join(SRC, "tests", "**", "test_*.py"), recursive=True):
        if re.search(r"tests\.helpers|from helpers|import helpers", open(path).read()):
            helper_mods.add(os.path.relpath(path, SRC).replace(os.sep, "/"))
    fix = {}
    for nid, i in index.items():
        mod = nid.split("::")[0]
        paths = set(tests_fix.get(nid, ()))
        paths |= set(mods_fix.get(mod, ()))
        for p in paths:
            fix.setdefault(p, set()).add(i)
    # collection-time reads also affect every module that imports tests.helpers
    for hm, ps in mods_fix.items():
        for nid, i in index.items():
            if nid.split("::")[0] in helper_mods:
                for p in ps:
                    fix.setdefault(p, set()).add(i)
    fixdir, fixall = {}, set()
    rx = re.compile(r"\b(%s)\b" % "|".join(sorted(names, key=len, reverse=True)))
    for p, s in fix.items():
        fixdir.setdefault(os.path.dirname(p), set()).update(s)
        fixall |= s
        try:
            text = open(os.path.join(SRC, p)).read().lower()
            for n in set(rx.findall(text)):
                mentions[n] |= s
        except Exception:
            pass

    ids_out = [None] * len(index)
    for nid, i in index.items():
        ids_out[i] = nid
    m = {
        "v": 1,
        "ids": ids_out,
        "units": units,
        "filehit": filehit,
        "fix": {k: sorted(v) for k, v in fix.items()},
        "fixdir": {k: sorted(v) for k, v in fixdir.items()},
        "fixall": sorted(fixall),
        "mentions": {k: sorted(v) for k, v in mentions.items()},
        "dialects": names,
    }
    with gzip.open(OUT, "wt", compresslevel=6) as fh:
        json.dump(m, fh, separators=(",", ":"))
    log(f"map: {len(ids_out)} ids, {len(units)} files, {len(fix)} fixtures, "
        f"{os.path.getsize(OUT) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
