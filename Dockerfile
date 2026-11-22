# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Build-time map: fetch sqlglot at the pinned commit, run its tests under per-test
# coverage, write /app/map.json.gz. Kept before selector.py so editing the selector
# does not re-run the tests.
COPY requirements.lock build_map.py fixture_tracker.py /app/
RUN pip install --no-cache-dir -r /app/requirements.lock pytest-cov \
 && python /app/build_map.py \
 && rm -rf /tmp/cov /tmp/blast \
 && chmod -R a+rX /app /opt/sqlglot

COPY selector.py /app/selector.py
RUN chmod a+r /app/selector.py
CMD ["python", "/app/selector.py"]