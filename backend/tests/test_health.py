"""Liveness, and the guard that would have caught it being broken.

The two route tests here are the SCAFFOLD'S OWN, kept byte-identical from
before Elite's `app/` was ported over the top of it. They were red from S1
until 2026-08-31 and were read as a cosmetic mismatch — "open decision 5, the
scaffold expects a service name the ported app does not use". They were not
cosmetic. They were reporting that **every deploy fails its health gate**, and
nothing in the repository connected those two facts.

That is what `test_the_paths_the_deploy_scripts_poll_are_served` exists for.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# The scaffold's contract — unchanged since the repo was created
# --------------------------------------------------------------------------- #
def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_root_returns_service_name():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "digycorp-cx-agent"


# --------------------------------------------------------------------------- #
# THE GUARD. This is the test whose absence let a broken deploy ship.
# --------------------------------------------------------------------------- #
def test_the_paths_the_deploy_scripts_poll_are_served():
    """A path a deploy script curls MUST be a path this app routes.

    Both gates treat a non-2xx as "roll back", so a path that 404s does not
    degrade the deploy — it fails it, on a service that started perfectly.

    READ OUT OF THE SCRIPTS RATHER THAN LISTED HERE, deliberately. A hardcoded
    list is a second copy of the same fact and drifts from the scripts silently,
    which is the exact shape of the bug this guards: `remote-deploy.sh` asked
    for `/health` for months while `main.py` served `/api/health`, and both
    files were individually correct.
    """
    scripts = {
        "scripts/remote-deploy.sh": r"curl[^\n|]*?http://127\.0\.0\.1:8000(/[^\s>\"']*)",
        # Elite's own, inherited at S2 and not wired (open decision 2) — checked
        # anyway, because "not wired yet" is how the other one got here.
        "deploy/inherited/deploy-core.sh": r"curl[^\n|]*?\$\{(?:FQDN|CUSTOM_DOMAIN)\}(/[^\s>\"']*)",
    }

    served = {
        route.path for route in app.routes if hasattr(route, "path")
    }
    checked = 0

    for relative, pattern in scripts.items():
        path = REPO_ROOT / relative
        if not path.exists():          # inherited tree may be pruned
            continue
        for polled in re.findall(pattern, path.read_text(encoding="utf-8")):
            checked += 1
            assert polled in served, (
                f"{relative} polls {polled!r}, which this app does not serve. "
                f"Every deploy using that script will fail its health gate on a "
                f"healthy service. Served paths: {sorted(served)[:12]}..."
            )

    assert checked, "found no health polls to check — did the scripts move?"


# --------------------------------------------------------------------------- #
# Both paths, one answer
# --------------------------------------------------------------------------- #
def test_both_health_paths_report_the_same_thing():
    """Two scripts poll two paths (see main.health_root). They must not be able
    to disagree about whether the service is up."""
    bare = client.get("/health").json()
    under_api = client.get("/api/health").json()
    assert bare == under_api


def test_the_api_path_kept_the_keys_elites_script_reads():
    """`deploy/inherited/deploy-core.sh` polls /api/health and reads the BODY.
    `status` was added beside `api` and `database`; neither was replaced."""
    body = client.get("/api/health").json()
    assert body["api"] == "ok"
    assert body["database"] == "ok"


# --------------------------------------------------------------------------- #
# A database that is down is reported, and still deploys
# --------------------------------------------------------------------------- #
def test_a_dead_database_is_reported_honestly_but_still_answers_200():
    """ALWAYS 200 — see main._liveness. A non-2xx here fails the deploy, and a
    database outage is not a deployment fault; it wants a different person.
    `status` carries the truth for anything that reads the body."""
    class _Boom:
        def execute(self, *_a, **_kw):
            raise RuntimeError("connection refused")

        def close(self):
            pass

    with patch("app.main.SessionLocal", return_value=_Boom()):
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"
    assert "connection refused" in body["detail"]
