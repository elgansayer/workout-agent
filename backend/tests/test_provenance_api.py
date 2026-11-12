"""API tests: corrections, lineage, and provenance in UI/AI-facing responses."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from database import (
    get_body_metrics,
    get_or_create_user,
    init_db,
    save_body_metrics,
    save_daily_log,
    save_dashboard_insight,
    save_progress,
    save_workout,
)
from hevy_parser import parse_workout
from provenance import Provenance
from session_helpers import signed_session_cookie
from starlette.testclient import TestClient

_SECRET = "provenance-test-secret-that-is-long-enough"
LINEAGE_ROUTE = "/api/provenance/{record_type}/{record_key}"

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
DAY = "2026-01-01"


def _session_cookie(webapp_app: Any, user_id: str) -> tuple[str, str]:
    session_id = webapp_app.SESSION_STORE.create(user_id)
    return (
        webapp_app.SESSION_SECURITY.cookie.name,
        signed_session_cookie(
            _SECRET,
            user="alice@example.com",
            user_id=user_id,
            session_id=session_id,
        ),
    )


def _google(**kw: Any) -> Provenance:
    return Provenance.imported(
        "google_health", "google-health-body/1", ingested_at=NOW, **kw
    )


def _ai(version: str) -> Provenance:
    return Provenance.derived("ai:gemini/test", version, ingested_at=NOW)


class _Harness:
    def __init__(self, client: TestClient, db: str, alice: str, bob: str, state: dict[str, str]):
        self.client = client
        self.db = db
        self.alice = alice
        self.bob = bob
        self._state = state

    def act_as(self, user_id: str) -> None:
        self._state["user"] = user_id


@pytest.fixture
def harness(tmp_path: Any, monkeypatch: Any) -> _Harness:
    db = str(tmp_path / "provenance_api.db")
    init_db(db)
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "provenance-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "provenance-client-secret")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)

    import webapp.app as webapp_app

    importlib.reload(webapp_app)
    alice = get_or_create_user("alice@example.com", "Alice", db_path=db)["id"]
    bob = get_or_create_user("bob@example.com", "Bob", db_path=db)["id"]
    from consent_ledger import ConsentPurpose, grant_consent_at

    for user_id in (alice, bob):
        grant_consent_at(
            db, user_id, ConsentPurpose.AI_PROCESSING, actor_id=user_id
        )
    state = {"user": alice}
    monkeypatch.setattr(webapp_app, "_check_api_auth", lambda request: state["user"])
    client = TestClient(webapp_app.app)
    cookie_name, cookie_value = _session_cookie(webapp_app, alice)
    client.cookies.set(cookie_name, cookie_value)
    return _Harness(client, db, alice, bob, state)


def _seed_bad_reading(h: _Harness) -> None:
    save_body_metrics(
        {"weight_kg": 91.0, "body_fat_pct": 15.0},
        DAY,
        h.db,
        user_id=h.alice,
        provenance=_google(source_timestamp="2026-01-01T06:30:00Z"),
    )


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def test_correction_preserves_the_original_and_returns_the_lineage(harness: _Harness) -> None:
    _seed_bad_reading(harness)

    response = harness.client.post(
        "/api/body-metrics/corrections",
        json={"date": DAY, "weight_kg": 81.0, "reason": "Scale was on carpet"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reading"]["weight_kg"] == 81.0
    assert body["reading"]["body_fat_pct"] == 15.0
    assert body["provenance"]["kind"] == "user_entered"
    assert body["provenance"]["label"] == "Corrected by you"
    revisions = body["lineage"]
    assert [r["revision"] for r in revisions] == [2, 1]
    assert revisions[1]["kind"] == "imported" and revisions[1]["source"] == "google_health"
    assert revisions[1]["content"]["values"]["weight_kg"] == 91.0
    assert get_body_metrics(db_path=harness.db, user_id=harness.alice)[0]["weight_kg"] == 81.0


def test_corrected_reading_is_visible_to_the_ui_with_its_origin(harness: _Harness) -> None:
    _seed_bad_reading(harness)
    save_body_metrics(
        {"weight_kg": 80.0}, "2026-01-02", harness.db, user_id=harness.alice, provenance=_google()
    )
    harness.client.post("/api/body-metrics/corrections", json={"date": DAY, "weight_kg": 81.0})

    readings = harness.client.get("/api/progress").json()["body_readings"]

    assert [r["date"] for r in readings] == ["2026-01-02", DAY]  # newest first
    assert readings[0]["provenance"]["kind"] == "imported"
    assert readings[1]["provenance"]["kind"] == "user_entered"
    assert readings[1]["provenance"]["corrected"] is True


def test_lineage_endpoint_returns_every_revision_for_the_owner(harness: _Harness) -> None:
    _seed_bad_reading(harness)
    harness.client.post("/api/body-metrics/corrections", json={"date": DAY, "weight_kg": 81.0})
    # The scheduled import re-sends the bad value; the correction stays current.
    save_body_metrics(
        {"weight_kg": 91.0, "body_fat_pct": 15.0}, DAY, harness.db, user_id=harness.alice,
        provenance=_google(),
    )

    response = harness.client.get(LINEAGE_ROUTE.format(record_type="body_metrics", record_key=DAY))

    assert response.status_code == 200
    body = response.json()
    assert body["record_type"] == "body_metrics" and body["record_key"] == DAY
    assert body["current"]["kind"] == "user_entered" and body["current"]["applied"] is True
    assert [(r["revision"], r["applied"]) for r in body["revisions"]] == [(3, False), (2, True), (1, True)]


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"date": DAY, "weight_kg": 5}, "between"),
        ({"date": DAY, "weight_kg": 9000}, "between"),
        ({"date": DAY, "body_fat_pct": -3}, "between"),
        ({"date": DAY}, "at least one"),
        ({"date": "2999-01-01", "weight_kg": 80}, "future"),
        ({"date": DAY, "weight_kg": 91.0}, "does not change"),
    ],
)
def test_invalid_corrections_return_422_and_change_nothing(
    harness: _Harness, payload: dict[str, Any], needle: str
) -> None:
    _seed_bad_reading(harness)
    response = harness.client.post("/api/body-metrics/corrections", json=payload)
    assert response.status_code == 422
    assert needle in json.dumps(response.json())
    assert get_body_metrics(db_path=harness.db, user_id=harness.alice)[0]["weight_kg"] == 91.0
    lineage = harness.client.get(LINEAGE_ROUTE.format(record_type="body_metrics", record_key=DAY)).json()["revisions"]
    assert len(lineage) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"date": "yesterday", "weight_kg": 80},
        {"date": "2026-1-1", "weight_kg": 80},
        {"date": DAY, "weight_kg": "heavy"},
        {"date": DAY, "weight_kg": 80, "reason": "x" * 281},
        {"date": DAY, "weight_kg": 80, "unexpected": 1},
        {"weight_kg": 80},
    ],
)
def test_malformed_correction_bodies_are_rejected(harness: _Harness, payload: dict[str, Any]) -> None:
    response = harness.client.post("/api/body-metrics/corrections", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("claim", ["user_id", "owner_id", "account_id"])
def test_the_owner_is_never_taken_from_the_request_body(harness: _Harness, claim: str) -> None:
    """A client-supplied owner claim cannot redirect a correction to another account.

    The mutation-ownership middleware rejects the claim with a non-enumerating 404
    before the route runs; the request model also forbids unknown fields.
    """
    save_body_metrics({"weight_kg": 90.0}, DAY, harness.db, user_id=harness.bob, provenance=_google())
    response = harness.client.post(
        "/api/body-metrics/corrections",
        json={"date": DAY, "weight_kg": 50.0, claim: harness.bob},
    )
    assert response.status_code in (404, 422)
    assert get_body_metrics(db_path=harness.db, user_id=harness.bob)[0]["weight_kg"] == 90.0
    assert get_body_metrics(db_path=harness.db, user_id=harness.alice) == []


def test_a_correction_applies_only_to_the_authenticated_users_reading(harness: _Harness) -> None:
    save_body_metrics({"weight_kg": 90.0}, DAY, harness.db, user_id=harness.bob, provenance=_google())
    harness.act_as(harness.alice)
    response = harness.client.post("/api/body-metrics/corrections", json={"date": DAY, "weight_kg": 70.0})
    assert response.status_code == 200
    assert response.json()["revision"] == 1  # Alice has no prior reading: a manual entry
    assert get_body_metrics(db_path=harness.db, user_id=harness.bob)[0]["weight_kg"] == 90.0


# ---------------------------------------------------------------------------
# Lineage endpoint isolation and input handling
# ---------------------------------------------------------------------------


def test_lineage_is_not_visible_to_another_user(harness: _Harness) -> None:
    _seed_bad_reading(harness)
    harness.act_as(harness.bob)
    other = harness.client.get(LINEAGE_ROUTE.format(record_type="body_metrics", record_key=DAY))
    missing = harness.client.get("/api/provenance/body_metrics/2020-02-02")
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()  # non-enumerating


@pytest.mark.parametrize(
    "path",
    [
        "/api/provenance/not_a_family/2026-01-01",
        "/api/provenance/body_metrics/has%20space",
        "/api/provenance/body_metrics/%3Cscript%3E",
        "/api/provenance/body_metrics/" + "a" * 201,
        "/api/provenance/users/anything",
        "/api/provenance/record_provenance/anything",
    ],
)
def test_lineage_endpoint_rejects_unknown_families_and_unsafe_keys(harness: _Harness, path: str) -> None:
    response = harness.client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Record not found"}  # same body as a missing record


def test_new_routes_require_authentication(tmp_path: Any, monkeypatch: Any) -> None:
    db = str(tmp_path / "anon.db")
    init_db(db)
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "provenance-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "provenance-client-secret")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    import webapp.app as webapp_app

    importlib.reload(webapp_app)
    client = TestClient(webapp_app.app)
    assert client.get(LINEAGE_ROUTE.format(record_type="body_metrics", record_key=DAY)).status_code == 401
    assert client.post("/api/body-metrics/corrections", json={"date": DAY, "weight_kg": 80}).status_code == 401


# ---------------------------------------------------------------------------
# UI-facing responses distinguish source data, corrections and recommendations
# ---------------------------------------------------------------------------


def test_history_marks_daily_plans_as_derived_recommendations(harness: _Harness) -> None:
    save_daily_log(
        DAY, 1, "Legs", "high", "Squat 5x5", "life", harness.db, user_id=harness.alice,
        provenance=_ai("daily-plan/1"),
    )
    save_daily_log(
        "2026-01-02", 2, "Pull", "low", "Row 4x8", "life", harness.db, user_id=harness.alice
    )

    logs = harness.client.get("/api/history").json()["logs"]

    by_date = {log["date"]: log["provenance"] for log in logs}
    assert by_date[DAY]["kind"] == "derived" and by_date[DAY]["authoritative"] is False
    assert by_date[DAY]["label"] == "AI recommendation"
    assert by_date[DAY]["transformation_version"] == "daily-plan/1"
    assert by_date["2026-01-02"]["label"] == "Derived by the app"  # unattributed writer


def test_dashboard_marks_the_coach_insight_as_a_derived_recommendation(harness: _Harness) -> None:
    save_dashboard_insight(
        '{"fatigue": "Green", "wins_stalls": "ok", "advice": "go"}',
        harness.db,
        user_id=harness.alice,
        provenance=_ai("dashboard-insight/1"),
    )

    data = harness.client.get("/api/dashboard").json()

    assert data["dashboard_insight"]["advice"] == "go"
    assert data["dashboard_insight_provenance"]["kind"] == "derived"
    assert data["dashboard_insight_provenance"]["authoritative"] is False
    assert data["dashboard_insight_provenance"]["source_label"] == "AI coach (Gemini test)"


def test_dashboard_without_an_insight_has_no_provenance_to_claim(harness: _Harness) -> None:
    data = harness.client.get("/api/dashboard").json()
    assert data["dashboard_insight"] is None
    assert data["dashboard_insight_provenance"] is None


def test_another_users_insight_provenance_is_not_shown(harness: _Harness) -> None:
    save_dashboard_insight(
        '{"fatigue": "Red", "wins_stalls": "x", "advice": "rest"}',
        harness.db,
        user_id=harness.bob,
        provenance=_ai("dashboard-insight/1"),
    )
    data = harness.client.get("/api/dashboard").json()
    assert data["dashboard_insight_provenance"] is None


class _NamedProvider:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def name(self) -> str:
        return "Gemini (gemini-test)"

    def generate(self, prompt: str, *, stream: bool = False) -> Any:
        self.prompts.append(prompt)
        return iter(["Coach reply."]) if stream else "Because volume dropped."


def _use_provider(monkeypatch: Any, provider: _NamedProvider) -> None:
    import webapp.app as webapp_app
    from coaching_capabilities import CapabilityItem, CoachingCapabilityState
    from config import Config

    capabilities = CoachingCapabilityState(
        provider_ready=True,
        provider_name=provider.name(),
        capabilities={
            "workout_analysis": CapabilityItem(True, "ready", ""),
            "recovery_analysis": CapabilityItem(True, "ready", ""),
            "body_composition": CapabilityItem(True, "ready", ""),
            "programme_guidance": CapabilityItem(True, "ready", ""),
        },
        suggested_prompts=[],
        overall_status="fully_grounded",
        disclaimer="",
        welcome_message="",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-key")
    monkeypatch.setattr(webapp_app, "resolve_provider", lambda **kw: provider)
    monkeypatch.setattr(webapp_app, "get_config", lambda: Config.load())
    monkeypatch.setattr(
        webapp_app, "get_coaching_capabilities", lambda *a, **kw: capabilities
    )


def test_explanations_are_saved_and_returned_as_ai_derived(harness: _Harness, monkeypatch: Any) -> None:
    provider = _NamedProvider()
    _use_provider(monkeypatch, provider)
    path = "/api/xai_reasoning/2026-01-02_Deadlift%20(Barbell)"

    first = harness.client.get(path).json()
    again = harness.client.get(path).json()

    for body in (first, again):
        assert body["reasoning"] == "Because volume dropped."
        assert body["provenance"]["kind"] == "derived"
        assert body["provenance"]["source"] == "ai:gemini/gemini-test"
        assert body["provenance"]["transformation_version"] == "xai-reasoning/1"
    assert len(provider.prompts) == 1  # the second call is served from storage


def test_coach_retrieval_labels_source_data_corrections_and_recommendations(
    harness: _Harness, monkeypatch: Any
) -> None:
    payload = {
        "workouts": [
            {
                "id": "hevy-w-9",
                "title": "Pull",
                "start_time": "2026-01-01T09:00:00Z",
                "exercises": [{"title": "Row", "sets": [{"weight_kg": 60, "reps": 8}]}],
            }
        ]
    }
    save_workout(payload, harness.db, when=DAY, user_id=harness.alice)
    summary = parse_workout(payload)
    assert summary is not None
    save_progress(summary, harness.db, user_id=harness.alice)
    _seed_bad_reading(harness)
    harness.client.post(
        "/api/body-metrics/corrections",
        json={"date": DAY, "weight_kg": 81.0, "reason": "PRIVATE-REASON-TEXT"},
    )
    save_body_metrics({"weight_kg": 80.5}, "2026-01-02", harness.db, user_id=harness.alice, provenance=_google())
    save_daily_log(
        DAY, 1, "Legs", "high", "Squat 5x5", "life", harness.db, user_id=harness.alice,
        provenance=_ai("daily-plan/1"),
    )
    provider = _NamedProvider()
    _use_provider(monkeypatch, provider)

    response = harness.client.get("/api/rag_search?q=How+is+my+deadlift+progressing%3F")

    assert response.status_code == 200
    [prompt] = provider.prompts
    assert "Provenance rules:" in prompt
    context = json.loads(
        prompt.split("Verified training data context")[1]
        .split("\n", 1)[1]
        .rsplit("\n\nUser's question:", 1)[0]
    )
    biometrics = {row["date"]: row["provenance"] for row in context["biometrics"]}
    assert biometrics[DAY]["origin"] == "user_entered"
    assert biometrics[DAY]["corrected_by_user"] is True
    assert biometrics[DAY]["authoritative"] is True
    assert biometrics["2026-01-02"]["origin"] == "imported"
    assert biometrics["2026-01-02"]["source"] == "Google Health"
    assert context["logs"][0]["provenance"]["origin"] == "derived"
    assert context["logs"][0]["provenance"]["authoritative"] is False
    assert context["data_sources"]["history"]["origin"] == "imported"
    assert context["data_sources"]["history"]["sessions_with_provenance"] == 1
    assert context["data_sources"]["prs"]["origin"] == "derived"
    # Identifiers and private free text never reach the model.
    assert "PRIVATE-REASON-TEXT" not in prompt
    assert "hevy-w-9" not in prompt


def test_coach_retrieval_does_not_present_unrecorded_records_as_verified(
    harness: _Harness, monkeypatch: Any
) -> None:
    import sqlite3

    with sqlite3.connect(harness.db) as conn:  # stored before provenance tracking
        conn.execute(
            "INSERT INTO body_metrics (date, weight_kg, user_id) VALUES (?, 80.0, ?)",
            (DAY, harness.alice),
        )
    provider = _NamedProvider()
    _use_provider(monkeypatch, provider)

    harness.client.get("/api/rag_search?q=How+is+my+deadlift+progressing%3F")

    [prompt] = provider.prompts
    assert '"origin": "unrecorded"' in prompt
    assert '"authoritative": false' in prompt


def test_corrections_are_csrf_protected_in_the_production_stack(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The route inherits CSRF, ownership and auth boundaries from the secure entrypoint."""
    db = str(tmp_path / "secure.db")
    init_db(db)
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("WEB_PUBLIC_URL", raising=False)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "provenance-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "provenance-client-secret")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)

    import webapp.app as webapp_app
    from webapp import secure_app

    importlib.reload(webapp_app)
    importlib.reload(secure_app)
    alice = get_or_create_user("alice@example.com", "Alice", db_path=db)["id"]
    save_body_metrics({"weight_kg": 91.0}, DAY, db, user_id=alice, provenance=_google())
    client = TestClient(secure_app.app, base_url="http://testserver")
    cookie_name, cookie_value = _session_cookie(webapp_app, alice)
    client.cookies.set(cookie_name, cookie_value)
    payload = {"date": DAY, "weight_kg": 81.0}

    assert client.post("/api/body-metrics/corrections", json=payload).status_code == 403
    assert get_body_metrics(db_path=db, user_id=alice)[0]["weight_kg"] == 91.0

    token = client.get("/api/csrf-token").json()["token"]
    ok = client.post(
        "/api/body-metrics/corrections", json=payload, headers={"X-CSRF-Token": token}
    )
    assert ok.status_code == 200 and ok.json()["provenance"]["corrected"] is True
    assert get_body_metrics(db_path=db, user_id=alice)[0]["weight_kg"] == 81.0

    replay = client.post(
        "/api/body-metrics/corrections",
        json={"date": DAY, "weight_kg": 82.0},
        headers={"X-CSRF-Token": token},
    )
    assert replay.status_code == 403  # tokens are single use
    assert get_body_metrics(db_path=db, user_id=alice)[0]["weight_kg"] == 81.0

    anonymous = TestClient(secure_app.app, base_url="http://testserver")
    assert anonymous.get(LINEAGE_ROUTE.format(record_type="body_metrics", record_key=DAY)).status_code in (
        302,
        401,
    )
