# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# map.json.gz was generated once by build_map.py (with fixture_tracker.py) inside this same
# base image and is committed as a plain file. The build only fetches sqlglot at the pinned commit.
COPY requirements.lock build_map.py fixture_tracker.py /app/
RUN pip install --no-cache-dir -r /app/requirements.lock pytest-cov \
 && cd /app && python -c "import build_map; build_map.fetch()" \
 && chmod -R a+rX /app /opt/sqlglot

COPY map.json.gz /app/map.json.gz
COPY selector.py /app/selector.py
RUN chmod a+r /app/map.json.gz /app/selector.py
CMD ["python", "/app/selector.py"]