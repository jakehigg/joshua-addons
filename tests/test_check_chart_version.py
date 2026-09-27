"""Tests for scripts/check_chart_version.py: the version-agreement guard."""

from __future__ import annotations

from scripts import check_chart_version


def test_the_committed_tree_agrees_on_one_version() -> None:
    assert check_chart_version.main() == 0


def test_a_disagreeing_version_fails(tmp_path, monkeypatch, capsys) -> None:
    chart = tmp_path / "Chart.yaml"
    chart.write_text("version: 0.0.1\nappVersion: '0.0.2'\n")
    addons_dir = tmp_path / "addons"
    (addons_dir / "sample").mkdir(parents=True)
    (addons_dir / "sample" / "docker-compose.yml").write_text(
        "services:\n  sample:\n    image: x:${JOSHUA_ADDONS_VERSION:-0.0.1}\n"
    )
    monkeypatch.setattr(check_chart_version, "ROOT", tmp_path)
    monkeypatch.setattr(check_chart_version, "CHART", chart)
    monkeypatch.setattr(check_chart_version, "ADDONS_DIR", addons_dir)
    assert check_chart_version.main() == 1
    assert "not the same everywhere" in capsys.readouterr().err


def test_a_missing_value_fails(tmp_path, monkeypatch, capsys) -> None:
    chart = tmp_path / "Chart.yaml"
    chart.write_text("version: 0.0.1\n")  # no appVersion
    monkeypatch.setattr(check_chart_version, "CHART", chart)
    monkeypatch.setattr(check_chart_version, "ADDONS_DIR", tmp_path / "addons")
    assert check_chart_version.main() == 1
    assert "cannot read" in capsys.readouterr().err


def _tree(tmp_path, monkeypatch, compose: str, values: str | None = None):
    """A chart at 0.1.3 and one addon with ``compose`` and, if given, ``values``."""
    chart = tmp_path / "Chart.yaml"
    chart.write_text("version: 0.1.3\nappVersion: '0.1.3'\n")
    addon = tmp_path / "addons" / "sample"
    addon.mkdir(parents=True)
    (addon / "docker-compose.yml").write_text(compose)
    if values is not None:
        (addon / "values.yaml").write_text(values)
    monkeypatch.setattr(check_chart_version, "ROOT", tmp_path)
    monkeypatch.setattr(check_chart_version, "CHART", chart)
    monkeypatch.setattr(check_chart_version, "ADDONS_DIR", tmp_path / "addons")


COMPOSE_OK = (
    "services:\n  sample:\n    image: x:${JOSHUA_ADDONS_VERSION:-0.1.3}\n"
    "    environment:\n"
    "      WORKER_IMAGE: ${WORKER_IMAGE:-ghcr.io/o/worker:${JOSHUA_ADDONS_VERSION:-0.1.3}}\n"
)


def test_a_worker_pin_that_matches_passes(tmp_path, monkeypatch, capsys) -> None:
    values = "env:\n  WORKER_IMAGE: ghcr.io/o/worker:0.1.3\n"
    _tree(tmp_path, monkeypatch, COMPOSE_OK, values)
    assert check_chart_version.main() == 0
    assert "clean (0.1.3)" in capsys.readouterr().out


def test_a_stale_worker_pin_in_values_fails(tmp_path, monkeypatch, capsys) -> None:
    values = "env:\n  WORKER_IMAGE: ghcr.io/o/worker:0.1.2\n"
    _tree(tmp_path, monkeypatch, COMPOSE_OK, values)
    assert check_chart_version.main() == 1
    err = capsys.readouterr().err
    assert "not the same everywhere" in err
    assert "values.yaml env.WORKER_IMAGE tag" in err


def test_a_stale_worker_pin_in_compose_fails(tmp_path, monkeypatch, capsys) -> None:
    compose = COMPOSE_OK.replace(
        "worker:${JOSHUA_ADDONS_VERSION:-0.1.3}", "worker:${JOSHUA_ADDONS_VERSION:-0.1.2}"
    )
    _tree(tmp_path, monkeypatch, compose)
    assert check_chart_version.main() == 1
    assert "docker-compose.yml WORKER_IMAGE tag" in capsys.readouterr().err


def test_the_env_list_form_in_compose_is_read(tmp_path, monkeypatch, capsys) -> None:
    compose = (
        "services:\n  sample:\n    image: x:${JOSHUA_ADDONS_VERSION:-0.1.3}\n"
        "    environment:\n"
        "      - WORKER_IMAGE=ghcr.io/o/worker:${JOSHUA_ADDONS_VERSION:-0.1.1}\n"
    )
    _tree(tmp_path, monkeypatch, compose)
    assert check_chart_version.main() == 1
    assert "0.1.1" in capsys.readouterr().err


def test_a_literal_worker_tag_in_compose_is_read(tmp_path, monkeypatch) -> None:
    compose = COMPOSE_OK.replace(
        "${WORKER_IMAGE:-ghcr.io/o/worker:${JOSHUA_ADDONS_VERSION:-0.1.3}}",
        "ghcr.io/o/worker:0.0.9",
    )
    _tree(tmp_path, monkeypatch, compose)
    assert check_chart_version.main() == 1


def test_a_worker_image_without_a_tag_fails(tmp_path, monkeypatch, capsys) -> None:
    values = "env:\n  WORKER_IMAGE: localhost:5000/o/worker\n"
    _tree(tmp_path, monkeypatch, COMPOSE_OK, values)
    assert check_chart_version.main() == 1
    assert "cannot read" in capsys.readouterr().err


def test_an_addon_without_a_worker_pin_is_not_checked(tmp_path, monkeypatch) -> None:
    compose = "services:\n  sample:\n    image: x:${JOSHUA_ADDONS_VERSION:-0.1.3}\n"
    _tree(tmp_path, monkeypatch, compose, "env:\n  OTHER: value\n")
    assert check_chart_version.main() == 0
