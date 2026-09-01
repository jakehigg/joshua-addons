"""Refuse a tree where the version is not the same in every file that holds it.

One version covers the whole repository. The chart, and every addon's image
tag, ship together, so a chart or a compose file that meets an image it did
not ship with is a broken install. Two things must agree:

* ``charts/joshua-addon/Chart.yaml`` ``version`` and ``appVersion``
* the ``JOSHUA_ADDONS_VERSION`` default in every ``addons/*/docker-compose.yml``

The release workflow packages the chart and tags every addon image from the
Git tag, so a release is right whatever these files say. This check keeps the
files honest, because a person who installs from a checkout gets what is
written here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "charts" / "joshua-addon" / "Chart.yaml"
ADDONS_DIR = ROOT / "addons"


def _compose_default(compose: Path) -> str | None:
    """The fallback in ``${JOSHUA_ADDONS_VERSION:-<default>}``."""
    match = re.search(r"\$\{JOSHUA_ADDONS_VERSION:-([^}]+)\}", compose.read_text())
    return match.group(1).strip() if match else None


def main() -> int:
    chart = yaml.safe_load(CHART.read_text())
    found = {
        "charts/joshua-addon/Chart.yaml version": str(chart.get("version", "")),
        "charts/joshua-addon/Chart.yaml appVersion": str(chart.get("appVersion", "")),
    }
    for compose in sorted(ADDONS_DIR.glob("*/docker-compose.yml")):
        name = compose.relative_to(ROOT).as_posix()
        found[f"{name} JOSHUA_ADDONS_VERSION default"] = _compose_default(compose)

    missing = [name for name, value in found.items() if not value]
    if missing:
        print("chart-version: cannot read " + ", ".join(missing), file=sys.stderr)
        return 1
    if len(set(found.values())) != 1:
        print("chart-version: the version is not the same everywhere.", file=sys.stderr)
        for name, value in found.items():
            print(f"  {value:>12}  {name}", file=sys.stderr)
        print("Set all of them to the version you release.", file=sys.stderr)
        return 1
    print(f"chart-version: clean ({next(iter(found.values()))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
