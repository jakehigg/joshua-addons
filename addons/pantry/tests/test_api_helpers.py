"""Unit tests for the small pure helpers in ``joshua_pantry.api``."""

from __future__ import annotations

import json

from joshua_pantry import api
from starlette.applications import Starlette


def test_int_param_defaults_when_absent() -> None:
    assert api._int_param(None, 100, minimum=1, maximum=500) == 100


def test_int_param_rejects_out_of_range() -> None:
    assert api._int_param("501", 100, minimum=1, maximum=500) is None
    assert api._int_param("0", 0, minimum=1) is None


def test_int_param_rejects_non_numeric() -> None:
    assert api._int_param("nope", 0, minimum=0) is None


def test_int_param_accepts_a_valid_value() -> None:
    assert api._int_param("42", 0, minimum=0, maximum=500) == 42


def test_detail_shapes_the_error_body() -> None:
    response = api._detail(404, "Item not found")
    assert response.status_code == 404
    assert json.loads(response.body) == {"detail": "Item not found"}


def test_build_api_app_is_a_starlette_app_with_the_expected_routes() -> None:
    app = api.build_api_app()
    assert isinstance(app, Starlette)
    paths = {route.path for route in app.routes}
    assert "/inventory" in paths
    assert "/purchases/{purchase_id:int}" in paths
    assert "/items/{item_id:int}" in paths
