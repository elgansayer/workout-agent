"""Inject representative secrets at every known leak site; none may reach the logs.

Each test drives the real production function with a failing dependency whose
error text carries synthetic secrets, then inspects the captured log output.
All values are synthetic and assembled from fragments.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from synthetic_secrets import (
    CLIENT_SECRET,
    EMAIL,
    GOOGLE_CODE,
    GOOGLE_REFRESH,
    HEALTH_SENTENCE,
    OPENAI_KEY,
    PUSH_ENDPOINT,
    TELEGRAM_TOKEN,
    USER_ID,
    USER_PROMPT,
)

from log_redaction import pseudonymise_identifier


def _response(status: int, text: str = "") -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = text.encode()
    return response


def test_telegram_failure_never_logs_the_bot_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import telegram_notifier

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise requests.HTTPError(
            f"401 Client Error: Unauthorized for url: {url}", response=_response(401)
        )

    monkeypatch.setattr(telegram_notifier.requests, "post", fail)

    with caplog.at_level(logging.DEBUG):
        sent = telegram_notifier.send_telegram_message(TELEGRAM_TOKEN, "12345", "hello")

    assert sent is False
    assert TELEGRAM_TOKEN not in caplog.text
    assert "HTTPError" in caplog.text and "401" in caplog.text


def test_push_failure_never_logs_the_subscription_endpoint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import push_notifier

    failure = push_notifier.WebPushException(
        f"Push failed: 410 Gone for {PUSH_ENDPOINT}",
        response=_response(410, json.dumps({"endpoint": PUSH_ENDPOINT, "reason": "gone"})),
    )
    monkeypatch.setattr(push_notifier, "webpush", MagicMock(side_effect=failure))

    with caplog.at_level(logging.DEBUG):
        delivered = push_notifier.send_push_notification(
            {"endpoint": PUSH_ENDPOINT, "keys": {"p256dh": "k", "auth": "a"}},
            {"title": "t"},
            "synthetic-vapid-private-key",
        )

    assert delivered is False
    assert "SyntheticEndpointToken" not in caplog.text
    assert "410" in caplog.text


def test_google_token_exchange_failure_never_logs_code_or_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import google_health_auth

    body = f"invalid_grant code={GOOGLE_CODE} client_secret={CLIENT_SECRET}"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise requests.HTTPError(
            f"400 Client Error for {body}", response=_response(400, body)
        )

    monkeypatch.setattr(google_health_auth.requests, "post", fail)

    with caplog.at_level(logging.DEBUG):
        tokens = google_health_auth.exchange_code("client-id", CLIENT_SECRET, GOOGLE_CODE)

    assert tokens is None
    assert GOOGLE_CODE not in caplog.text and CLIENT_SECRET not in caplog.text
    assert "HTTP status 400" in caplog.text


def test_google_token_exchange_failure_does_not_write_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import google_health_auth

    def fail(*_args: object, **_kwargs: object) -> None:
        raise requests.HTTPError("boom", response=_response(400, f"code={GOOGLE_CODE}"))

    monkeypatch.setattr(google_health_auth.requests, "post", fail)

    google_health_auth.exchange_code("client-id", CLIENT_SECRET, GOOGLE_CODE)

    captured = capsys.readouterr()
    assert GOOGLE_CODE not in captured.err and GOOGLE_CODE not in captured.out


def test_google_health_refresh_failure_scrubs_echoed_tokens(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import google_health_client

    def fail(*_args: object, **_kwargs: object) -> None:
        raise requests.ConnectionError(
            f"gateway echoed refresh_token={GOOGLE_REFRESH}&client_secret={CLIENT_SECRET}"
        )

    monkeypatch.setattr(google_health_client.requests, "post", fail)

    with caplog.at_level(logging.DEBUG):
        result = google_health_client._refresh_tokens("id", CLIENT_SECRET, GOOGLE_REFRESH)

    assert result is None
    assert GOOGLE_REFRESH not in caplog.text and CLIENT_SECRET not in caplog.text
    assert "token refresh failed" in caplog.text


def test_weather_failure_never_logs_the_users_coordinates(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import weather

    def fail(url: str, **_kwargs: object) -> None:
        raise requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")

    monkeypatch.setattr(weather.requests, "get", fail)

    with caplog.at_level(logging.DEBUG):
        assert weather.get_current_weather(lat=51.5085, lon=-0.1257) is None

    assert "51.5085" not in caplog.text and "0.1257" not in caplog.text
    assert "Could not fetch weather data" in caplog.text


def test_ai_generation_failure_never_logs_the_provider_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gemini_engine import generate_next_workout
    from program import BLOCKS

    provider = MagicMock()
    provider.generate.side_effect = RuntimeError(
        f"401 Unauthorized: Incorrect API key provided: {OPENAI_KEY}"
    )

    with caplog.at_level(logging.DEBUG):
        plan = generate_next_workout(provider, day=1, week=3, block=BLOCKS[1])

    assert plan  # falls back to the baseline plan
    assert OPENAI_KEY not in caplog.text
    assert "AI generation failed" in caplog.text


def _insight_config(tmp_path: Path) -> MagicMock:
    config = MagicMock()
    config.database_path = str(tmp_path / "insight.db")
    return config


def test_unusable_ai_insight_response_is_logged_as_metadata_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import insight_cron

    provider = MagicMock()
    provider.generate.return_value = json.dumps(
        {"summary": f"{HEALTH_SENTENCE}. {USER_PROMPT}."}
    )
    provider.name.return_value = "Synthetic"
    monkeypatch.setattr(insight_cron, "_resolve_insight_user", lambda path: USER_ID)
    monkeypatch.setattr(insight_cron, "resolve_provider", lambda **kw: provider)
    monkeypatch.setattr(insight_cron, "get_body_metrics", lambda **kw: [])
    monkeypatch.setattr(insight_cron, "get_daily_logs", lambda **kw: [])
    monkeypatch.setattr(insight_cron, "save_dashboard_insight", lambda *a, **kw: None)

    with caplog.at_level(logging.DEBUG):
        insight_cron.generate_daily_header(_insight_config(tmp_path))

    assert "Invalid JSON structure returned" in caplog.text
    assert "resting heart rate" not in caplog.text
    assert "knee" not in caplog.text


def test_scheduler_failure_logs_a_pseudonym_not_the_user_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import scheduler

    def explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError(f"coaching failed for {EMAIL}")

    monkeypatch.setattr(scheduler, "get_all_users", lambda _db: [{"id": USER_ID}])
    monkeypatch.setattr("main.run", explode)

    with caplog.at_level(logging.DEBUG):
        result = scheduler.run_coaching(MagicMock(database_path="synthetic.db"))

    assert result == 1
    assert USER_ID not in caplog.text
    assert EMAIL not in caplog.text
    assert pseudonymise_identifier(USER_ID) in caplog.text


def test_scheduler_loop_and_insight_jobs_log_pseudonyms_not_user_ids(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import scheduler

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler, "get_all_users", lambda _db: [{"id": USER_ID}])
    monkeypatch.setattr("insight_cron.generate_daily_header", explode)
    monkeypatch.setattr("insight_cron.generate_weekly_correlations", explode)
    config = MagicMock(database_path="synthetic.db")

    with caplog.at_level(logging.DEBUG):
        scheduler.run_daily_insight(config)
        scheduler.run_weekly_correlations(config)

    assert USER_ID not in caplog.text
    assert caplog.text.count(pseudonymise_identifier(USER_ID)) >= 2


def test_main_run_logs_a_pseudonym_not_the_user_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import main

    class _Stop(Exception):
        pass

    def stop(*_args: object, **_kwargs: object) -> None:
        raise _Stop

    config = MagicMock(database_path=str(tmp_path / "main.db"))
    monkeypatch.setattr(main.Config, "load", staticmethod(lambda: config))
    monkeypatch.setattr(main, "init_db", lambda path: None)
    monkeypatch.setattr(main, "_scope_config_to_user", lambda cfg, uid: cfg)
    monkeypatch.setattr(main, "_sync_hevy_routines", stop)

    with caplog.at_level(logging.INFO, logger="workout_agent"), pytest.raises(_Stop):
        main.run(preview=True, user_id=USER_ID)

    assert "Running daily cycle for user" in caplog.text
    assert USER_ID not in caplog.text
    assert pseudonymise_identifier(USER_ID) in caplog.text
