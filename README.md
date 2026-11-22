# Blast Radius selector (sqlglot)

Given a patch to sqlglot (pinned commit `eb11aec`), pick the tests that can catch it and split
them over up to 8 workers.

## How it works
- **Build time** (`build_map.py`, run by the `Dockerfile`): fetch sqlglot at the pinned commit,
  run its tests under per-test coverage, and write `/app/map.json.gz`:
  test <-> function, test <-> file, test <-> fixture file, test <-> dialect name.
- **Run time** (`selector.py`): parse each patch, map touched old-side lines to functions, and
  select the tests that executed them. Class/module-level edits widen to every test that touched
  the file plus tests mentioning that dialect. Comment/whitespace-only edits (identical AST)
  run nothing. Anything unanalysable (syntax error, rename, delete, root config) runs everything.
- Worker count minimises `busiest worker seconds + 2 s * workers`.

No table from patches to failing tests is used; the maps come only from the pinned commit.

## Files
| File | Purpose |
| --- | --- |
| `Dockerfile` | builds the image and the map |
| `build_map.py`, `fixture_tracker.py` | build-time map generation |
| `selector.py` | the program the scorer runs |
| `diag.py` | local per-commit misses report |
| `run_sample.ps1` / `Makefile` | run the 30 sample commits locally (PowerShell / make) |
| `score.py`, `SCORING.md`, `sample/` | from the starter kit |

## Run locally (PowerShell)
    .\run_sample.ps1
