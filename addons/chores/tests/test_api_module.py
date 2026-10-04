"""The helpers of ``joshua_chores.api``, called directly.

Query parsing, the bearer, the error map, and the route table.
"""

from __future__ import annotations

import pytest
from joshua_chores import api, service
from starlette.requests import Request
from starlette.routing import Route


def _request(query: str = "", headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": query.encode(),
        "headers": raw_headers,
    }
    return Request(scope)


@pytest.mark.parametrize("raw", ["", "overdue_only=", "overdue_only=false", "overdue_only=0"])
def test_flag_is_false_when_absent_or_false(raw: str) -> None:
    assert api._flag(_request(raw), "overdue_only") is False


@pytest.mark.parametrize("raw", ["overdue_only=true", "overdue_only=1", "overdue_only=TRUE"])
def test_flag_is_true_for_true_values(raw: str) -> None:
    assert api._flag(_request(raw), "overdue_only") is True


def test_flag_refuses_another_word() -> None:
    with pytest.raises(api.BadRequest, match="overdue_only must be true or false"):
        api._flag(_request("overdue_only=maybe"), "overdue_only")


def test_limit_takes_the_default_when_absent() -> None:
    assert api._limit(_request(), 50) == 50


def test_limit_reads_a_value_in_range() -> None:
    assert api._limit(_request("limit=7"), 50) == 7


@pytest.mark.parametrize("raw", ["0", "-1", "abc", str(api.ROWS_MAX + 1)])
def test_limit_refuses_a_value_out_of_range(raw: str) -> None:
    with pytest.raises(api.BadRequest, match="limit must be a whole number"):
        api._limit(_request(f"limit={raw}"), 50)


def test_bearer_reads_the_token_after_the_prefix() -> None:
    assert api._bearer(_request(headers={"Authorization": "Bearer abc"})) == "abc"


@pytest.mark.parametrize("value", ["", "Basic abc", "bearer abc"])
def test_bearer_is_empty_without_the_exact_prefix(value: str) -> None:
    headers = {"Authorization": value} if value else None
    assert api._bearer(_request(headers=headers)) == ""


def test_error_response_maps_each_service_error() -> None:
    assert api._error_response(service.NotFound("gone")).status_code == 404
    cooldown = api._error_response(service.Cooldown("wait", retry_after=42))
    assert cooldown.status_code == 409
    assert cooldown.headers["retry-after"] == "42"
    assert api._error_response(service.InvalidArgument("bad")).status_code == 400
    assert api._error_response(api.BadRequest("bad")).status_code == 400


def test_detail_body_shape() -> None:
    response = api._detail(418, "teapot")
    assert response.status_code == 418
    assert response.body == b'{"detail":"teapot"}'


def test_route_table_guards_only_the_manager_routes() -> None:
    routes = api._routes()
    assert all(isinstance(r, Route) for r in routes)
    by_key = {(r.path, m): r for r in routes for m in r.methods or ()}
    assert ("/chores/{chore_id:int}/complete", "POST") in by_key
    assert ("/transactions", "GET") in by_key
    assert ("/members", "POST") in by_key
    assert ("/chores/{chore_id:int}", "PUT") in by_key
    open_count = sum(1 for r in routes if r.endpoint.__name__ == "wrapped")
    guarded_count = sum(1 for r in routes if r.endpoint.__name__ == "guarded")
    assert open_count == 7
    assert guarded_count == 8


def test_build_api_app_mounts_every_route() -> None:
    app = api.build_api_app()
    assert len(app.routes) == len(api._routes())
