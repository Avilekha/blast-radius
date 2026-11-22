"""Blast Radius selector.

For each patch: find the old-side lines it touches, map them to functions, and run the
tests that executed those functions (per-test coverage map built at image build time).
Module/class-level edits widen to every test that touched the file, plus tests that mention
that dialect. Anything we cannot analyse falls back to running everything.
"""
import ast
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
MAP_PATH = os.environ.get("MAP_PATH", "/app/map.json.gz")
SRC = Path(os.environ.get("SRC_DIR", "/opt/sqlglot"))
MAX_WORKERS, WORKER_MS = 8, 2000
SWEEP_S = 5.0  # seconds: slower tests count as sweeps (see py_file)
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIALECT_FILE = re.compile(r"^sqlglot/(?:dialects|generators|parsers|typing)/([a-z0-9_]+)\.py$")
ALL_ROOT = {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py",
            "Makefile", "MANIFEST.in", ".python-version"}
NOTHING_PREFIX = (".github/", "docs/", "posts/", "benchmarks/", "sqlglotrs/")
NOTHING_SUFFIX = (".md", ".rst")


# One long-lived helper: imports sqlglot once, then forks per patch to see if the patched module still imports.
CHECKER = """
import importlib, importlib.abc, importlib.util, json, os, signal, sys
root = sys.argv[1]
out = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)
sys.path.insert(0, root)
import sqlglot

class Finder(importlib.abc.MetaPathFinder):
    def __init__(self, mod, tmp, is_pkg):
        self.mod, self.tmp, self.is_pkg = mod, tmp, is_pkg
    def find_spec(self, name, path, target=None):
        if name == self.mod:
            locs = [os.path.join(root, *self.mod.split("."))] if self.is_pkg else None
            return importlib.util.spec_from_file_location(name, self.tmp, submodule_search_locations=locs)

for line in sys.stdin:
    req = json.loads(line)
    pid = os.fork()
    if pid == 0:
        try:
            signal.alarm(30)
            sys.meta_path.insert(0, Finder(req["mod"], req["tmp"], req["pkg"]))
            sys.modules.pop(req["mod"], None)
            importlib.import_module(req["mod"])
            os._exit(0)
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    out.write("ok\\n" if os.waitstatus_to_exitcode(status) == 0 else "fail\\n")
    out.flush()
"""


def log(*a):
    print(*a, file=sys.stderr)


class AllTests(Exception):
    pass


class FilePatch:
    def __init__(self):
        self.old = self.new = None
        self.hunks = []  # (old_start, old_len, [lines])
        self.binary = False


def _path(s):
    s = s.split("\t")[0].strip()
    if s == "/dev/null":
        return None
    return s[2:] if s[:2] in ("a/", "b/") else s


def parse_patch(text):
    lines = text.split("\n")
    out, cur, i = [], None, 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            cur = FilePatch()
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            out.append(cur)
        elif cur is not None:
            if ln.startswith("--- "):
                cur.old = _path(ln[4:])
            elif ln.startswith("+++ "):
                cur.new = _path(ln[4:])
            elif ln.startswith("rename from "):
                cur.old = ln[12:]
            elif ln.startswith("rename to "):
                cur.new = ln[10:]
            elif ln.startswith("new file mode"):
                cur.old = None
            elif ln.startswith("deleted file mode"):
                cur.new = None
            elif ln.startswith("Binary files") or ln.startswith("GIT binary patch"):
                cur.binary = True
            else:
                m = HUNK.match(ln)
                if m:
                    start = int(m.group(1))
                    ol = int(m.group(2)) if m.group(2) is not None else 1
                    nl = int(m.group(4)) if m.group(4) is not None else 1
                    body, o, n = [], 0, 0
                    i += 1
                    while (o < ol or n < nl) and i < len(lines):
                        l = lines[i]
                        c = l[:1] or " "
                        if c == " ":
                            o += 1
                            n += 1
                        elif c == "-":
                            o += 1
                        elif c == "+":
                            n += 1
                        elif c == "\\":
                            i += 1
                            continue
                        else:
                            break
                        body.append(l if l else " ")
                        i += 1
                    cur.hunks.append((start, ol, body))
                    continue
        i += 1
    return out


def apply_hunks(old, hunks):
    out, cur = [], 0
    for start, ol, body in hunks:
        s0 = start if ol == 0 else start - 1
        out += old[cur:s0]
        for l in body:
            c = l[0]
            if c in " +":
                out.append(l[1:])
        cur = s0 + ol
    out += old[cur:]
    return out


def changed_lines(hunks):
    """-> removed old line numbers, insertion positions (before old line p), texts."""
    removed, inserts, rtxt, atxt = set(), set(), [], []
    for start, ol, body in hunks:
        ln = (start if ol == 0 else start - 1) + 1
        for l in body:
            c = l[0]
            if c == " ":
                ln += 1
            elif c == "-":
                removed.add(ln)
                rtxt.append(l[1:])
                ln += 1
            else:
                if l[1:].strip():
                    inserts.add((ln, len(l) - 1 - len(l[1:].lstrip())))
                atxt.append(l[1:])
    return removed, inserts, rtxt, atxt


def _norm(lines):
    out = []
    for l in lines:
        l = re.sub(r"\s*#.*$", "", l).strip()
        if l:
            out.append(l)
    return out


def _indent(l):
    return len(l) - len(l.lstrip())


def classify(units, ln):
    body, hdr = None, []
    for u in units:
        if u[1] <= ln <= u[2]:
            if body is None or (u[2] - u[1]) < (body[2] - body[1]):
                body = u
        elif u[0] <= ln < u[1]:
            hdr.append(u)
    res = list(hdr)
    if body is not None:
        res.append(body)
    return res, body is None


def inside(units, p):
    best = None
    for u in units:
        if u[1] <= p <= u[2] and (best is None or (u[2] - u[1]) < (best[2] - best[1])):
            best = u
    return best


class Sel:
    def __init__(self, M, tests):
        self.M = M
        self.n = len(tests)
        self.sec = {t["number"]: t["seconds"] for t in tests}
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        id2n = {t["id"]: t["number"] for t in tests}
        self.i2n = [id2n.get(i) for i in M["ids"]]
        known = {x for x in self.i2n if x is not None}
        self.always = {t["number"] for t in tests if t["number"] not in known}
        self.dialects = set(M["dialects"])
        self.noimport = set(M.get("noimport", []))
        self.testimp = M.get("testimp", {})
        self.last_risky = False
        self.checker = None
        self._sweeps = None

    def import_ok(self, path, new_text):
        """Would the patched module still import? Edits that crash at import fail nearly every test."""
        if path in self.noimport:
            return True
        mod = path[:-3].replace("/", ".")
        is_pkg = mod.endswith(".__init__")
        if is_pkg:
            mod = mod[: -len(".__init__")]
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", dir="/tmp", delete=False) as fh:
                tmp = fh.name
                fh.write(new_text)
            if self.checker is None or self.checker.poll() is not None:
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(SRC))
                self.checker = subprocess.Popen([sys.executable, "-c", CHECKER, str(SRC)], env=env, text=True,
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)
            self.checker.stdin.write(json.dumps({"mod": mod, "tmp": tmp, "pkg": is_pkg}) + "\n")
            self.checker.stdin.flush()
            ok = self.checker.stdout.readline().strip() == "ok"
            if not ok:
                log(f"import check failed for {mod}")
            return ok
        except Exception as e:
            log("import check error:", e)
            self.checker = None
            return False
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def close(self):
        if self.checker is not None:
            try:
                self.checker.stdin.close()
                self.checker.wait(timeout=10)
            except Exception:
                self.checker.kill()

    def conv(self, lst):
        i2n = self.i2n
        return {i2n[i] for i in lst if i2n[i] is not None}

    def py_file(self, path, fp):
        M = self.M
        self.last_risky = False
        old_src = (SRC / path).read_text()
        new_lines = apply_hunks(old_src.split("\n"), fp.hunks)
        try:
            new_tree = ast.parse("\n".join(new_lines))
        except (SyntaxError, ValueError):
            raise AllTests("syntax error in " + path)
        removed, inserts, rtxt, atxt = changed_lines(fp.hunks)
        if _norm(rtxt) == _norm(atxt):
            if ast.dump(ast.parse(old_src)) == ast.dump(new_tree):
                return set()
        units = M["units"].get(path, [])
        sel, top = set(), False
        hdr_hit = any(u[0] <= ln < u[1] for ln in removed for u in units)  # decorators, defaults: run at import
        for ln in removed:
            us, t = classify(units, ln)
            top |= t
            for u in us:
                sel |= self.conv(u[3])
        old_lines = old_src.split("\n")
        for p, ind in inserts:
            u = inside(units, p)
            if u is None:
                cands = [c for c in units if c[2] == p - 1 and _indent(old_lines[c[0] - 1]) < ind]
                if cands:
                    u = max(cands, key=lambda c: _indent(old_lines[c[0] - 1]))
            if u is None:
                top = True
            else:
                sel |= self.conv(u[3])
        self.last_risky = top or hdr_hit
        if self.last_risky and path.startswith("sqlglot/") and not self.import_ok(path, "\n".join(new_lines)):
            raise AllTests("patched module no longer imports: " + path)
        dm = DIALECT_FILE.match(path)
        if top:
            fh = self.conv(M["filehit"].get(path, []))
            if dm and dm.group(1) in self.dialects:
                named = set()
                for n in {dm.group(1)} | set(M.get("desc", {}).get(dm.group(1), ())):
                    named |= self.conv(M["mentions"].get(n, []))
                    named |= set(self.by_file.get(f"tests/dialects/test_{n}.py", []))
                # a slow test that merely ran some code in this file (it sweeps every dialect) but never
                # names this dialect is a poor bet for a class-level edit: it costs more than a miss risk
                slow = {n for n in self.sweeps() if self.sec[n] >= SWEEP_S}
                fh = {n for n in fh if self.sec[n] < SWEEP_S or n in named}
                fh |= named
                fh -= slow
            sel |= fh
        return sel

    def sweeps(self):
        """Tests that name at least half of all dialects (they loop over every dialect)."""
        if self._sweeps is None:
            cnt = {}
            for n in self.dialects:
                for i in self.M["mentions"].get(n, ()):
                    cnt[i] = cnt.get(i, 0) + 1
            need = max(3, len(self.dialects) // 2)
            self._sweeps = self.conv([i for i, c in cnt.items() if c >= need])
        return self._sweeps

    def file(self, fp):
        if fp.binary:
            raise AllTests("binary")
        path = fp.new or fp.old
        if fp.old and fp.new and fp.old != fp.new:
            raise AllTests("rename")
        base = os.path.basename(path)
        if path.startswith("sqlglot/"):
            if not path.endswith(".py") or fp.new is None or fp.old is None:
                if fp.old is None and path.endswith(".py"):
                    return set()  # brand-new module: only matters via edits elsewhere
                raise AllTests("sqlglot non-py or deleted")
            return self.py_file(path, fp)
        if path.startswith("tests/"):
            if path.startswith("tests/fixtures/"):
                M = self.M
                lst = M["fix"].get(path)
                if lst is None:
                    if not path.endswith(".sql"):
                        raise AllTests("unmapped non-sql fixture " + path)
                    lst = M["fixdir"].get(os.path.dirname(path)) or M["fixall"]
                return self.conv(lst)
            if path.endswith(".py"):
                if base in ("conftest.py", "__init__.py"):
                    raise AllTests("conftest/__init__")
                importers = self.testimp.get(path, [])
                if base.startswith("test_"):
                    out = set(self.by_file.get(path, []))
                    for f in importers:  # e.g. Validator, imported by every dialect test file
                        out |= set(self.by_file.get(f, []))
                    return out
                if fp.old is None or fp.new is None:
                    raise AllTests("tests helper added/deleted")
                out = self.py_file(path, fp)
                if self.last_risky:
                    for f in importers:
                        out |= set(self.by_file.get(f, []))
                return out
            raise AllTests("tests data file")
        if path.startswith(NOTHING_PREFIX) or (path.endswith(NOTHING_SUFFIX) and "/" not in path):
            return set()
        raise AllTests("root/unknown file " + path)


def split(sel, ms):
    if not sel:
        return []
    items = sorted(sel, key=lambda n: -ms[n])
    best = None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        loads, ws = [0] * k, [[] for _ in range(k)]
        for n in items:
            j = loads.index(min(loads))
            ws[j].append(n)
            loads[j] += ms[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return [w for w in best[1] if w]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = [0] * len(tests)
    for t in tests:
        ms[t["number"]] = round(t["seconds"] * 1000)
    everything = set(range(len(tests)))
    try:
        with gzip.open(MAP_PATH, "rt") as fh:
            sel = Sel(json.load(fh), tests)
    except Exception as e:
        log("map unavailable, running everything:", e)
        sel = None
    for patch in sorted((DATA / "commits").glob("*.patch")):
        chosen = everything
        if sel is not None:
            try:
                chosen = set()
                text = patch.read_text(errors="replace")
                fps = parse_patch(text)
                if text.strip() and (not fps or len(re.findall(r"^@@ -\d", text, re.M)) != sum(len(f.hunks) for f in fps)):
                    raise AllTests("patch not understood")
                for fp in fps:
                    chosen |= sel.file(fp)
                if chosen:
                    chosen |= sel.always
            except Exception as e:
                log(f"{patch.stem}: fallback to all ({e})")
                chosen = everything
        workers = split(chosen, ms)
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        log(f"{patch.stem}: {len(chosen)} tests, {sum(ms[n] for n in chosen) / 1000:.1f}s total")
    if sel is not None:
        sel.close()


if __name__ == "__main__":
    main()
