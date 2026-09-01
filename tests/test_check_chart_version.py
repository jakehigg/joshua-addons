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
