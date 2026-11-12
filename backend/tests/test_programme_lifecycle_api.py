"""HTTP surface for programme lifecycle, adaptation, overrides and drift (#975)."""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime, timedelta
from typing import Any

import database
import pytest
from hevy_reader import HevySourceSnapshot
from programme_fixtures import training_data, workout
from secret_vault import ResolvedCredential, SecretValue
from session_helpers import signed_session_cookie
from starlette.testclient import TestClient

_SECRET = "programme-lifecycle-test-secret-long-enough"


def _snapshot(data: Any) -> HevySourceSnapshot:
    return HevySourceSnapshot(
        data=data,
        status="ok",
        fetched_at=datetime.now(tz=UTC).isoformat(),
        warnings=[],
        counts={"routines": len(data.routines), "recent_workouts": 0},
    )


class Api:
    def __init__(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db = str(tmp_path / "api.db")
        database.init_db(self.db)
        for key, value in {
            "DATABASE_PATH": self.db,
            "APP_ENV": "test",
            "WEB_AUTH_SECRET": _SECRET,
            "WEB_GOOGLE_CLIENT_ID": "client-id",
            "WEB_GOOGLE_CLIENT_SECRET": "client-secret",
        }.items():
            monkeypatch.setenv(key, value)
        import webapp.app as app_module

        importlib.reload(app_module)
        self.app = app_module
        monkeypatch.setattr(app_module, "ALLOW_ANONYMOUS_WEB", True)
        monkeypatch.setattr(
            app_module,
            "_vault_credential",
            lambda *a, **k: ResolvedCredential(
                provider="hevy", version=1, api_key=SecretValue("fixture-hevy-key")
            ),
        )
        self.live = training_data()
        monkeypatch.setattr(
            app_module, "_load_hevy_snapshot_for_user", lambda *a, **k: _snapshot(self.live)
        )
        self.user = database.get_legacy_user_id(self.db)
        self.client = self.login(self.user)

    def login(self, user_id: str) -> TestClient:
        client = TestClient(self.app.app)
        session_id = self.app.SESSION_STORE.create(user_id)
        client.cookies.set(
            self.app.SESSION_SECURITY.cookie.name,
            signed_session_cookie(
                _SECRET, user=f"{user_id}@example.test", user_id=user_id, session_id=session_id
            ),
        )
        return client

    def activate(self, client: TestClient | None = None, *, start: date | None = None) -> int:
        client = client or self.client
        payload = {
            "selected_routine_ids": ["routine-a", "routine-b"],
            "duration_weeks": 8,
            "goal": "hypertrophy",
            "start_date": (start or datetime.now(tz=UTC).date()).isoformat(),
            "sessions_per_week": 4,
        }
        preview = client.post("/api/programmes/preview", json=payload).json()["preview"]
        response = client.post(
            "/api/programmes/activate", json={**payload, "preview_token": preview["preview_token"]}
        )
        assert response.status_code == 200, response.text
        return int(response.json()["active_programme"]["id"])

    def cache_workout(self, raw: dict[str, Any], user_id: str | None = None) -> None:
        database.save_workout(raw, self.db, raw["start_time"][:10], user_id=user_id or self.user)


@pytest.fixture()
def api(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Api:
    return Api(tmp_path, monkeypatch)


def _today_workout(workout_id: str, exercises: dict[str, list[float]], offset: int = 0) -> dict[str, Any]:
    day = datetime.now(tz=UTC) - timedelta(days=offset)
    raw = workout(workout_id, 1, exercises, rpe=None, stamp=day.replace(microsecond=0).isoformat())
    return raw


def test_active_state_reports_the_materialised_week(api: Api) -> None:
    assert api.client.get("/api/programmes/active/state").json()["setup_required"] is True
    programme_id = api.activate()
    body = api.client.get("/api/programmes/active/state").json()
    assert body["programme"]["id"] == programme_id and body["programme"]["status"] == "active"
    assert body["week"]["week_number"] == 1 and body["week"]["sessions"]
    assert body["week"]["next_exposure"]["routine_position"] == 1
    assert body["pending_decisions"] == [] and body["overrides"] == []
    assert body["revisions"][0]["reason"] == "activation"
    week = api.client.get(f"/api/programmes/{programme_id}/weeks/1").json()["week"]
    assert week["cadence"] == "rolling" and week["sessions"][0]["exercises"]
    assert api.client.get(f"/api/programmes/{programme_id}/weeks/99").status_code == 422
    nxt = api.client.get(f"/api/programmes/{programme_id}/next-session").json()
    assert nxt["week"] == 1 and nxt["next_exposure"]["routine_position"] == 1


def test_pause_resume_and_extend_through_the_api(api: Api) -> None:
    programme_id = api.activate()
    paused = api.client.post(f"/api/programmes/{programme_id}/pause").json()["programme"]
    assert paused["status"] == "paused"
    assert api.client.post(f"/api/programmes/{programme_id}/pause").status_code == 422
    resumed = api.client.post(f"/api/programmes/{programme_id}/resume", json={}).json()["programme"]
    assert resumed["status"] == "active" and resumed["duration_weeks"] == 8  # paused for no whole week
    extended = api.client.post(f"/api/programmes/{programme_id}/extend", json={"weeks": 2}).json()["programme"]
    assert extended["duration_weeks"] == 10 and extended["revision_reason"] == "extension"
    assert api.client.post(f"/api/programmes/{programme_id}/extend", json={"weeks": 20}).status_code == 422


def test_review_decisions_and_explanations_come_from_stored_inputs(api: Api) -> None:
    programme_id = api.activate()
    api.cache_workout(
        _today_workout("w1", {"bench": [10, 10], "row": [12, 12]}),
    )
    first = api.client.post(f"/api/programmes/{programme_id}/review", json={})
    assert first.status_code == 200, first.text
    review = first.json()["review"]
    assert review["evaluated_workouts"] == 1 and review["decisions"]
    again = api.client.post(f"/api/programmes/{programme_id}/review", json={}).json()["review"]
    assert again["replayed_workouts"] == 1 and again["decisions"] == []  # idempotent

    listed = api.client.get(f"/api/programmes/{programme_id}/decisions").json()["decisions"]
    assert {item["decision_type"] for item in listed} >= {"activation"}
    exercise = next(item for item in listed if item["exercise_template_id"])
    detail = api.client.get(
        f"/api/programmes/{programme_id}/decisions/{exercise['id']}"
    ).json()["decision"]
    assert detail["rationale_codes"] and detail["explanation"]
    assert detail["inputs"]["observation"]["workout_id"] == "w1"
    assert detail["rule_version"].startswith("programme-adaptation/")
    filtered = api.client.get(
        f"/api/programmes/{programme_id}/decisions", params={"exercise": "bench"}
    ).json()["decisions"]
    assert filtered and all(item["exercise_template_id"] == "bench" for item in filtered)
    assert api.client.get(f"/api/programmes/{programme_id}/decisions/99999").status_code == 404


def test_overrides_are_durable_visible_and_revocable(api: Api) -> None:
    programme_id = api.activate()
    created = api.client.post(
        f"/api/programmes/{programme_id}/overrides",
        json={"kind": "pin", "exercise_template_id": "bench", "value": {"fields": ["load"]}, "weeks": 2,
              "note": "keep the bar light"},
    )
    assert created.status_code == 200, created.text
    override = created.json()["override"]
    assert override["status"] == "active" and override["ends_on"]
    shown = api.client.get("/api/programmes/active/state").json()["overrides"]
    assert [item["id"] for item in shown] == [override["id"]]
    week = api.client.get(f"/api/programmes/{programme_id}/weeks/1").json()["week"]
    bench = next(e for e in week["sessions"][0]["exercises"] if e["template_id"] == "bench")
    assert bench["pinned_fields"] == ["load"]
    revoked = api.client.post(f"/api/programmes/{programme_id}/overrides/{override['id']}/revoke")
    assert revoked.json()["override"]["status"] == "revoked"
    listed = api.client.get(f"/api/programmes/{programme_id}/overrides").json()["overrides"]
    assert [item["status"] for item in listed] == ["revoked"]  # retained for audit
    assert api.client.post(f"/api/programmes/{programme_id}/overrides", json={"kind": "mystery"}).status_code == 422
    assert api.client.post(
        f"/api/programmes/{programme_id}/overrides",
        json={"kind": "pin", "exercise_template_id": "bench", "value": {"fields": ["load"]}, "weeks": 99},
    ).status_code == 422


def test_unknown_or_owner_fields_are_rejected(api: Api) -> None:
    programme_id = api.activate()
    response = api.client.post(
        f"/api/programmes/{programme_id}/overrides",
        json={"kind": "review_request", "user_id": "someone-else"},
    )
    assert response.status_code in {403, 404, 422}
    assert api.client.post(f"/api/programmes/{programme_id}/extend", json={"weeks": 1, "extra": 1}).status_code == 422
    assert [
        item for item in database.get_notifications(api.user, db_path=api.db)
    ]  # the activation notification is the only side effect


def test_confirming_a_workout_match_requires_a_cached_workout(api: Api) -> None:
    programme_id = api.activate()
    missing = api.client.post(
        f"/api/programmes/{programme_id}/workout-matches/confirm",
        json={"workout_id": "nope", "routine_position": 1},
    )
    assert missing.status_code == 422
    api.cache_workout(_today_workout("w7", {"bench": [10, 10]}))
    confirmed = api.client.post(
        f"/api/programmes/{programme_id}/workout-matches/confirm",
        json={"workout_id": "w7", "routine_position": 1},
    )
    assert confirmed.status_code == 200, confirmed.text
    decisions = confirmed.json()["review"]["decisions"]
    assert decisions and decisions[0]["inputs"]["match"]["user_confirmed"] is True
    bad = api.client.post(
        f"/api/programmes/{programme_id}/workout-matches/confirm",
        json={"workout_id": "w7", "routine_position": 9},
    )
    assert bad.status_code == 422


def test_decision_actions_confirm_reject_revert_and_override(api: Api) -> None:
    programme_id = api.activate()
    for index in range(4):
        api.cache_workout(
            _today_workout(f"w{index}", {"bench": [10, 10, 10], "row": [12, 12, 12]}, offset=0)
            | {"id": f"w{index}", "start_time": (datetime.now(tz=UTC) - timedelta(minutes=index)).replace(microsecond=0).isoformat(),
               "end_time": (datetime.now(tz=UTC) - timedelta(minutes=index)).replace(microsecond=0).isoformat()}
        )
    api.client.post(f"/api/programmes/{programme_id}/review", json={})
    decisions = api.client.get(f"/api/programmes/{programme_id}/decisions").json()["decisions"]
    applied = [d for d in decisions if d["application_state"] == "auto_applied" and d["exercise_template_id"]]
    assert applied, decisions
    target = next(d for d in applied if d["decision_type"] in {"progress_reps", "progress_load"})
    reverted = api.client.post(f"/api/programmes/{programme_id}/decisions/{target['id']}/revert")
    assert reverted.status_code == 200 and reverted.json()["decision"]["application_state"] == "reverted"
    again = api.client.post(f"/api/programmes/{programme_id}/decisions/{target['id']}/revert")
    assert again.status_code == 422  # already reverted
    assert api.client.post(
        f"/api/programmes/{programme_id}/decisions/{target['id']}/reject"
    ).status_code == 422  # only a pending decision can be rejected
    assert api.client.post(
        f"/api/programmes/{programme_id}/decisions/{target['id']}/override", json={"adjustment": {"sets": 9}}
    ).status_code == 422


def test_drift_is_shown_exactly_and_resolved_only_by_an_explicit_choice(api: Api) -> None:
    programme_id = api.activate()
    api.live.routines[0].title = "Upper A (renamed)"
    drift = api.client.get(f"/api/programmes/{programme_id}/drift").json()["drift"]
    entry = next(item for item in drift["routines"] if item["routine_id"] == "routine-a")
    assert entry["state"] == "renamed" and entry["changes"][0]["kind"] == "routine_renamed"
    assert drift["writes_to_hevy"] is False
    state = api.client.get("/api/programmes/active/state").json()
    assert [r["reason"] for r in state["revisions"]] == ["activation"]  # detection changed nothing

    kept = api.client.post(
        f"/api/programmes/{programme_id}/drift/resolve",
        json={"resolutions": [{"routine_id": "routine-a", "action": "keep"}]},
    ).json()["result"]
    assert kept["revision_created"] is False

    stale = api.client.post(
        f"/api/programmes/{programme_id}/drift/resolve",
        json={"resolutions": [{"routine_id": "routine-a", "action": "rebase", "expected_hash": "stale"}]},
    )
    assert stale.status_code == 409

    done = api.client.post(
        f"/api/programmes/{programme_id}/drift/resolve",
        json={"resolutions": [{"routine_id": "routine-a", "action": "rebase", "expected_hash": entry["current_hash"]}]},
    )
    assert done.status_code == 200 and done.json()["result"]["revision_created"] is True
    after = api.client.get(f"/api/programmes/{programme_id}/drift").json()["drift"]
    assert after["has_drift"] is False
    revisions = api.client.get("/api/programmes/active/state").json()["revisions"]
    assert [item["reason"] for item in revisions] == ["activation", "rebase"]
    bad = api.client.post(
        f"/api/programmes/{programme_id}/drift/resolve",
        json={"resolutions": [{"routine_id": "routine-a", "action": "explode"}]},
    )
    assert bad.status_code == 422


def test_every_route_is_tenant_scoped_and_authenticated(api: Api) -> None:
    programme_id = api.activate()
    other_user = database.get_or_create_user("athlete-b@example.test", "B", api.db)["id"]
    other = api.login(other_user)
    assert other.get("/api/programmes/active/state").json()["setup_required"] is True
    for method, path, body in (
        ("get", f"/api/programmes/{programme_id}/weeks/1", None),
        ("get", f"/api/programmes/{programme_id}/next-session", None),
        ("get", f"/api/programmes/{programme_id}/decisions", None),
        ("get", f"/api/programmes/{programme_id}/overrides", None),
        ("get", f"/api/programmes/{programme_id}/drift", None),
        ("post", f"/api/programmes/{programme_id}/pause", None),
        ("post", f"/api/programmes/{programme_id}/resume", {}),
        ("post", f"/api/programmes/{programme_id}/extend", {"weeks": 1}),
        ("post", f"/api/programmes/{programme_id}/review", {}),
        ("post", f"/api/programmes/{programme_id}/overrides", {"kind": "review_request"}),
        ("post", f"/api/programmes/{programme_id}/drift/resolve",
         {"resolutions": [{"routine_id": "routine-a", "action": "keep"}]}),
    ):
        api.app._RATE_LIMITS.clear()  # each probe is judged on tenancy, not throttling
        response = getattr(other, method)(path, **({"json": body} if body is not None else {}))
        assert response.status_code == 404, (method, path, response.status_code, response.text)
    # The owner's programme is untouched by every attempt above.
    assert api.client.get("/api/programmes/active/state").json()["programme"]["status"] == "active"

    anonymous = TestClient(api.app.app)
    assert anonymous.post(f"/api/programmes/{programme_id}/pause").status_code in {401, 403}
    assert anonymous.post(f"/api/programmes/{programme_id}/extend", json={"weeks": 1}).status_code in {401, 403}
