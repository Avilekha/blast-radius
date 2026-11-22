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
import time
import urllib.request

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
SRC = os.environ.get("SRC_DIR", "/opt/sqlglot")
OUT = os.environ.get("MAP_OUT", "/app/map.json.gz")
COV = "/tmp/cov/.coverage"
BLAST = "/tmp/blast"


T0 = time.time()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def tick(label):
    log(f"[timing] {time.time() - T0:6.0f}s since build_map started: {label}")


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
        # The tests size their process pools from the CPU count. Pin it to the grader's 4 so the
        # build behaves the same on any machine (a 22-core PC would otherwise start 22 workers per pool).
        PYTHON_CPU_COUNT="4",
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
        "--cov-report=", "--timeout=300", "--junitxml=/tmp/junit.xml",
    ]
    r = subprocess.run(cmd, cwd=SRC, env=env())
    log("pytest exit", r.returncode)


def durations():
    """nodeid (no params) -> seconds, from the main run's junit file."""
    import xml.etree.ElementTree as ET

    out = {}
    try:
        for tc in ET.parse("/tmp/junit.xml").getroot().iter("testcase"):
            parts = (tc.get("classname") or "").split(".")
            k = next((i for i, p in enumerate(parts) if p.startswith("test_")), None)
            if k is None:
                continue
            nid = "/".join(parts[: k + 1]) + ".py"
            nid += "".join("::" + p for p in parts[k + 1 :]) + "::" + (tc.get("name") or "").split("[")[0]
            out[nid] = max(out.get(nid, 0.0), float(tc.get("time") or 0))
    except Exception as e:
        log("no durations:", e)
    return out


def units_of(src):
    us = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ds = min([n.lineno] + [d.lineno for d in n.decorator_list])
            us.append([ds, n.body[0].lineno, n.end_lineno])
    return us


GROUP = "/tmp/grp"
HEAVY_S = 0.5   # seconds in the main (coverage, 4-way parallel) run; below this, tests share one run
MAX_HEAVY = 40
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


def run_cov(ids, tag, xdist):
    """Run the given tests with coverage that follows spawned worker processes."""
    import coverage

    d = os.path.join(GROUP, tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    rc = os.path.join(d, "rc")
    with open(rc, "w") as fh:
        fh.write("[run]\nparallel = True\npatch = subprocess\nsource =\n    sqlglot\n    tests\n")
    e = env()
    e["COVERAGE_FILE"] = os.path.join(d, ".coverage")
    cmd = [sys.executable, "-m", "coverage", "run", "--rcfile", rc, "-m", "pytest"]
    cmd += ["-n", "4"] if xdist else []
    cmd += ["-q", "-p", "no:cacheprovider", "--timeout=300", *ids]
    r = subprocess.run(cmd, cwd=SRC, env=e, stdout=sys.stderr)
    r2 = subprocess.run([sys.executable, "-m", "coverage", "combine", "--rcfile", rc, d],
                        cwd=SRC, env=e, stdout=sys.stderr, stderr=sys.stderr)
    log(f"[{tag}] pytest exit {r.returncode}, combine exit {r2.returncode}, tests {len(ids)}")
    cd = coverage.CoverageData(basename=e["COVERAGE_FILE"])
    cd.read()
    out = {}
    for f in cd.measured_files():
        rel = os.path.relpath(f, SRC).replace(os.sep, "/")
        out[rel] = set(cd.lines(f) or [])
    return out


def run_groups(ids_list, index):
    """-> [(lines per file, set of test indexes it is credited to)].

    Slow pool tests are run alone, so their coverage is exact. The rest share one run and
    their workers' lines are credited to all of them.
    """
    from concurrent.futures import ThreadPoolExecutor

    shutil.rmtree(GROUP, ignore_errors=True)
    os.makedirs(GROUP)
    pool = pool_tests(ids_list)
    dur = durations()
    slow = sorted((n for n in pool if dur.get(n, 0) >= HEAVY_S), key=lambda n: -dur[n])[:MAX_HEAVY]
    light = [n for n in pool if n not in slow]
    log("pool-using tests:", len(pool), "| run alone:", len(slow), "| shared:", len(light))
    for n in slow:
        log(f"  alone: {dur[n]:6.1f}s {n}")
    for n, t in sorted(dur.items(), key=lambda kv: -kv[1])[:8]:
        log(f"  slowest in main run: {t:6.1f}s {n}")

    shared = run_cov(light, "shared", True) if light else {}
    groups = [(shared, {index[n] for n in light})] if light else []
    tick("shared pool run finished")

    def one(a):
        i, n = a
        return n, run_cov([n], f"one{i}", False)

    with ThreadPoolExecutor(max_workers=4) as ex:
        for n, lines in ex.map(one, list(enumerate(slow))):
            nfiles = sum(1 for f, s in lines.items() if f.startswith("sqlglot/") and s)
            log(f"  alone result: {nfiles} sqlglot files measured for {n}")
            if nfiles < 5:
                raise SystemExit(f"coverage for {n} looks empty: failing the build")
            groups.append((lines, {index[n]}))
    # Sanity check: a function body that only the pool workers run must show up somewhere.
    chk = "sqlglot/optimizer/eliminate_joins.py"
    fn = next(n for n in ast.walk(ast.parse(open(os.path.join(SRC, chk)).read()))
              if isinstance(n, ast.FunctionDef) and n.name == "_should_eliminate_join")
    ran = [ln for g, _ in groups for ln in g.get(chk, ()) if fn.body[0].lineno <= ln <= fn.end_lineno]
    log("worker-only lines seen in _should_eliminate_join:", len(ran))
    if not ran:
        raise SystemExit("worker processes were not measured: failing the build rather than ship a bad map")
    return groups


def read_coverage(index, groups):
    import coverage

    cd = coverage.CoverageData(basename=COV)
    cd.read()
    units, filehit = {}, {}
    by_mod = {}
    for nid, i in index.items():
        by_mod.setdefault(nid.split("::")[0], set()).add(i)
    todo = {}
    for f in cd.measured_files():
        todo[os.path.relpath(f, SRC).replace(os.sep, "/")] = f
    for glines, _ in groups:
        for rel in glines:
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
        for glines, gset in groups:
            body = False
            for ln in glines.get(rel, ()):
                if ln < len(owner) and owner[ln] != -1:
                    hits[owner[ln]] |= gset
                    body = True  # import-time lines do not count: every test imports the file
            if body:
                fh |= gset
        try:
            by_line = cd.contexts_by_lineno(f)
        except Exception:
            by_line = {}
        for ln, ctxs in by_line.items():
            ids = set()
            for c in ctxs:
                nid, _, phase = c.rpartition("|")
                if not nid:
                    continue
                if nid not in index:
                    index[nid] = len(index)
                ids.add(index[nid])
                if phase in ("setup", "teardown"):
                    # setUpClass / module fixtures run inside the first test's setup but serve all of them
                    ids |= by_mod.get(nid.split("::")[0], set())
            fh |= ids
            if ln < len(owner) and owner[ln] != -1:
                hits[owner[ln]] |= ids
        units[rel] = [[u[0], u[1], u[2], sorted(h)] for u, h in zip(us, hits)]
        filehit[rel] = sorted(fh)
    return units, filehit


def dialect_descendants(names, root=None):
    """For each dialect file stem: every stem that (transitively) imports from it."""
    root = root or SRC
    dirs = ("dialects", "parsers", "generators", "typing")
    deps = {n: set() for n in names}
    for d in dirs:
        for p in glob.glob(os.path.join(root, "sqlglot", d, "*.py")):
            stem = os.path.basename(p)[:-3]
            if stem not in deps:
                continue
            try:
                tree = ast.parse(open(p).read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                parts = node.module.split(".")
                if parts[0] != "sqlglot" or len(parts) < 2 or parts[1] not in dirs:
                    continue
                if len(parts) >= 3 and parts[2] in deps and parts[2] != stem:
                    deps[stem].add(parts[2])
                if len(parts) == 2:
                    deps[stem] |= {a.name for a in node.names if a.name in deps and a.name != stem}
    kids = {n: {a for a in names if n in deps[a]} for n in names}
    out = {}
    for n in names:
        seen, todo = set(), [n]
        while todo:
            for k in kids[todo.pop()]:
                if k not in seen:
                    seen.add(k)
                    todo.append(k)
        out[n] = sorted(seen - {n})
    return out


def test_importers(root=None):
    """tests/x.py -> every tests file that (transitively) imports it."""
    root = root or SRC
    files = [os.path.relpath(p, root).replace(os.sep, "/")
             for p in glob.glob(os.path.join(root, "tests", "**", "*.py"), recursive=True)]
    have = set(files)

    def resolve(mod):
        for cand in (mod.replace(".", "/") + ".py", mod.replace(".", "/") + "/__init__.py"):
            if cand in have:
                return cand
        return None

    imports = {f: set() for f in files}
    for f in files:
        try:
            tree = ast.parse(open(os.path.join(root, f)).read())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods.append(n.module)
                mods += [n.module + "." + a.name for a in n.names]
            elif isinstance(n, ast.Import):
                mods += [a.name for a in n.names]
            for m in mods:
                r = resolve(m)
                if r and r != f:
                    imports[f].add(r)
    users = {f: set() for f in files}
    for f, deps in imports.items():
        for d in deps:
            users[d].add(f)
    out = {}
    for f in files:
        seen, todo = set(), [f]
        while todo:
            for u in users[todo.pop()]:
                if u not in seen:
                    seen.add(u)
                    todo.append(u)
        if seen:
            out[f] = sorted(seen)
    return out


def find_noimport():
    """sqlglot modules that do not import cleanly here; the selector skips its import check for them."""
    code = (
        "import importlib, json, os, sys\n"
        "root = sys.argv[1]; bad = []\n"
        "for dp, dn, fn in os.walk(os.path.join(root, 'sqlglot')):\n"
        "    for f in fn:\n"
        "        if not f.endswith('.py') or f == '__main__.py': continue\n"
        "        rel = os.path.relpath(os.path.join(dp, f), root)\n"
        "        mod = rel[:-3].replace(os.sep, '.')\n"
        "        if mod.endswith('.__init__'): mod = mod[:-9]\n"
        "        try: importlib.import_module(mod)\n"
        "        except BaseException: bad.append(rel.replace(os.sep, '/'))\n"
        "print('JSON' + json.dumps(sorted(bad)))\n"
    )
    r = subprocess.run([sys.executable, "-c", code, SRC], env=env(), capture_output=True, text=True)
    line = next((l for l in r.stdout.splitlines() if l.startswith("JSON")), "JSON[]")
    bad = json.loads(line[4:])
    log("modules that do not import cleanly:", bad)
    return bad


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
    tick('sqlglot fetched')
    ids_list = collect_ids()
    tick('tests collected')
    run_tests()
    tick('main coverage run finished')
    index = {nid: i for i, nid in enumerate(ids_list)}
    groups = run_groups(ids_list, index)
    tick('pool groups finished')
    units, filehit = read_coverage(index, groups)
    tick('coverage read')
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

    inst = {}
    for path in glob.glob(os.path.join(BLAST, "inst-*.json")):
        d = json.load(open(path))
        for nid, ks in d["tests"].items():
            if nid in index:
                for k in ks:
                    inst.setdefault(k, set()).add(index[nid])
        for mod, ks in d["mods"].items():  # created in collection / setup: serves every test of the module
            members = [i for n, i in index.items() if n.split("::")[0] == mod]
            for k in ks:
                inst.setdefault(k, set()).update(members)
    log("class-creation map:", len(inst), "classes")

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
        "desc": dialect_descendants(names),
        "testimp": test_importers(),
        "noimport": find_noimport(),
        "inst": {k: sorted(v) for k, v in inst.items()},
    }
    with gzip.open(OUT, "wt", compresslevel=6) as fh:
        json.dump(m, fh, separators=(",", ":"))
    tick('map written')
    log(f"map: {len(ids_out)} ids, {len(units)} files, {len(fix)} fixtures, "
        f"{os.path.getsize(OUT) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
