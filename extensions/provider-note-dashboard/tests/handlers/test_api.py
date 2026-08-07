"""Tests for the dashboard handler.

The two branching helpers carry the handler's real logic, the window resolver
and the scope builder. They read only their parameters and two attributes, so a
bare instance built without the SimpleAPI runtime, with those attributes set, is
enough to exercise every branch.
"""

import json
from http import HTTPStatus
from types import SimpleNamespace

import arrow

from provider_note_dashboard.handlers import api
from provider_note_dashboard.handlers.api import NoteDashboardAPI


def _handler():
    """Build a handler without running the SimpleAPI constructor."""
    return NoteDashboardAPI.__new__(NoteDashboardAPI)


def test_unknown_view_is_a_bad_request() -> None:
    resolved, error = _handler()._resolve_window({"view": "year"})
    assert resolved is None
    assert error is not None


def test_known_view_resolves_with_defaults() -> None:
    resolved, error = _handler()._resolve_window({"view": "day"})
    assert error is None
    assert resolved["strategy"].view_key == "day"
    assert resolved["zone"] == "UTC"
    assert resolved["periods"] == 14
    assert isinstance(resolved["anchor"], arrow.Arrow)


def test_default_view_is_week_when_none_is_given() -> None:
    resolved, error = _handler()._resolve_window({})
    assert error is None
    assert resolved["strategy"].view_key == "week"


def test_explicit_periods_override_the_default() -> None:
    resolved, _ = _handler()._resolve_window({"view": "day", "periods": "5"})
    assert resolved["periods"] == 5


def test_a_non_numeric_periods_falls_back_to_the_default() -> None:
    resolved, _ = _handler()._resolve_window({"view": "day", "periods": "abc"})
    assert resolved["periods"] == 14


def test_a_periods_below_one_is_ignored() -> None:
    resolved, _ = _handler()._resolve_window({"view": "day", "periods": "0"})
    assert resolved["periods"] == 14


def test_the_zone_and_anchor_are_read_from_the_query() -> None:
    resolved, _ = _handler()._resolve_window(
        {"view": "day", "tz": "America/New_York", "anchor": "2026-07-22"}
    )
    assert resolved["zone"] == "America/New_York"
    assert resolved["anchor"].format("YYYY-MM-DD") == "2026-07-22"


def _scoped_handler(header_key, allowlist):
    handler = _handler()
    handler.request = SimpleNamespace(headers={"canvas-logged-in-user-id": header_key})
    handler.secrets = {"lead_staff_keys": allowlist}
    return handler


def test_scope_is_cross_provider_when_the_allowlist_is_empty() -> None:
    scope = _scoped_handler("me", "")._scope()
    assert scope.cross_provider is True


def test_scope_is_cross_provider_for_a_listed_lead() -> None:
    scope = _scoped_handler("me", "me, someone-else")._scope()
    assert scope.cross_provider is True


def test_scope_is_self_only_for_a_non_lead() -> None:
    scope = _scoped_handler("me", "someone-else")._scope()
    assert scope.cross_provider is False
    assert scope.allows("me") is True
    assert scope.allows("someone-else") is False


def _route_handler(query, header_key, allowlist):
    handler = _handler()
    handler.request = SimpleNamespace(
        query_params=query,
        headers={"canvas-logged-in-user-id": header_key},
    )
    handler.secrets = {"lead_staff_keys": allowlist}
    return handler


def _body(response):
    return json.loads(response.content)


def test_data_route_filters_providers_to_the_caller_scope(mocker) -> None:
    mocker.patch.object(
        api,
        "aggregate_counts",
        return_value={
            "view": "day",
            "window": {"start": "s", "end": "e"},
            "buckets": [],
            "providers": [
                {"provider_id": "me", "total": 2, "counts": {}},
                {"provider_id": "other", "total": 5, "counts": {}},
            ],
        },
    )
    handler = _route_handler({"view": "day"}, "me", "someone-else")

    response = handler.data()[0]

    assert response.status_code == HTTPStatus.OK
    ids = [row["provider_id"] for row in _body(response)["providers"]]
    assert ids == ["me"]


def test_data_route_returns_the_window_error_for_an_unknown_view(mocker) -> None:
    spy = mocker.patch.object(api, "aggregate_counts")
    handler = _route_handler({"view": "year"}, "me", "")

    response = handler.data()[0]

    assert response.status_code == HTTPStatus.BAD_REQUEST
    spy.assert_not_called()


def test_detail_route_requires_a_provider_id() -> None:
    handler = _route_handler({"view": "day"}, "me", "")

    response = handler.detail()[0]

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_detail_route_forbids_a_provider_outside_the_scope(mocker) -> None:
    spy = mocker.patch.object(api, "build_detail")
    handler = _route_handler(
        {"view": "day", "provider_id": "other"}, "me", "someone-else"
    )

    response = handler.detail()[0]

    assert response.status_code == HTTPStatus.FORBIDDEN
    spy.assert_not_called()


def test_detail_route_returns_rows_for_an_allowed_provider(mocker) -> None:
    mocker.patch.object(
        api, "build_detail", return_value=[{"note_id": "n1"}]
    )
    handler = _route_handler(
        {"view": "day", "provider_id": "me"}, "me", "someone-else"
    )

    response = handler.detail()[0]

    assert response.status_code == HTTPStatus.OK
    body = _body(response)
    assert body["provider_id"] == "me"
    assert body["rows"] == [{"note_id": "n1"}]
