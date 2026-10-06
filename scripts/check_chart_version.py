"""Refuse a tree where the version is not the same in every file that holds it,
or is not of the date shape.

The version is a date: ``YYYY.M.N``, the year, the month with no leading zero,
and the sequence of the release in that month, from 1. ``2026.10.1`` is the
first release in October 2026. Two constraints give the shape: Helm requires
SemVer, so no part has a leading zero; and the version always has three parts,
because an unquoted ``2026.10`` is a number in YAML.

One version covers the whole repository. The chart, and every addon's image
tag, ship together, so a chart or a compose file that meets an image it did
not ship with is a broken install. These must agree:

* ``charts/joshua-addon/Chart.yaml`` ``version`` and ``appVersion``
* the ``JOSHUA_ADDONS_VERSION`` default in every ``addons/*/docker-compose.yml``
* the tag of a pinned ``WORKER_IMAGE``: ``env.WORKER_IMAGE`` in an
  ``addons/*/values.yaml`` and in a ``charts/joshua-addon/ci/*-values.yaml``,
  and the ``WORKER_IMAGE`` line in an ``addons/*/docker-compose.yml``. An
  addon that starts a second image pins its tag in these files, so the pin
  must move with each release.
* in every ``addons/*/.env.example``: the ``JOSHUA_ADDONS_VERSION=`` line, and
  a ``WORKER_IMAGE=`` line whose tag is a version. A placeholder tag, such
  as ``<version>``, is not a version and is not checked. A ``#`` before the
  name does not matter.

``docs/releasing.md`` lists these files for a version bump.

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

# YYYY.M.N: a four-digit year, a month 1-12 with no leading zero, a sequence from 1.
VERSION_SHAPE = re.compile(r"^\d{4}\.(1[0-2]|[1-9])\.[1-9]\d*$")
SHAPE_HELP = (
    "The version is YYYY.M.N: the year, the month with no leading zero, and the "
    "sequence of the release in that month, from 1. Example: 2026.10.1."
)


def version_shape_ok(version: str) -> bool:
    """True when ``version`` is of the ``YYYY.M.N`` shape."""
    return VERSION_SHAPE.match(version) is not None


ADDONS_DIR = ROOT / "addons"


def _compose_default(compose: Path) -> str | None:
    """The fallback in ``${JOSHUA_ADDONS_VERSION:-<default>}``."""
    match = re.search(r"\$\{JOSHUA_ADDONS_VERSION:-([^}]+)\}", compose.read_text())
    return match.group(1).strip() if match else None


def _image_tag(image: str) -> str | None:
    """The tag of ``repo/name:tag``, or None when the image has no tag."""
    name = image.rsplit("/", 1)[-1]
    if ":" not in name:
        return None
    return name.rsplit(":", 1)[1].strip() or None


def _compose_worker_tag(compose: Path) -> tuple[bool, str | None]:
    """Whether a compose file sets ``WORKER_IMAGE``, and the tag it pins.

    The tag is the ``JOSHUA_ADDONS_VERSION`` default on that line, as in
    ``WORKER_IMAGE: ${WORKER_IMAGE:-<image>:${JOSHUA_ADDONS_VERSION:-<tag>}}``
    or ``WORKER_IMAGE=<image>:${JOSHUA_ADDONS_VERSION:-<tag>}``, or else the
    literal tag of the image.
    """
    for line in compose.read_text().splitlines():
        text = line.split("#", 1)[0].strip().lstrip("- ")
        match = re.match(r"""["']?WORKER_IMAGE["']?\s*[:=]\s*(.+)""", text)
        if not match:
            continue
        value = match.group(1).strip().strip("\"'")
        default = re.search(r"\$\{JOSHUA_ADDONS_VERSION:-([^}]+)\}", value)
        if default:
            return True, default.group(1).strip()
        literal = re.sub(r"^\$\{WORKER_IMAGE:-(.*)\}$", r"\1", value)
        return True, _image_tag(literal)
    return False, None


def _values_worker_tag(values: Path) -> tuple[bool, str | None]:
    """Whether a values file sets ``env.WORKER_IMAGE``, and the tag it pins."""
    data = yaml.safe_load(values.read_text()) or {}
    env = data.get("env") if isinstance(data, dict) else None
    image = env.get("WORKER_IMAGE") if isinstance(env, dict) else None
    if not image:
        return False, None
    return True, _image_tag(str(image))


def _env_example_versions(env_file: Path) -> dict[str, str]:
    """The version mentions of an ``.env.example``, by name.

    ``JOSHUA_ADDONS_VERSION=<value>`` gives its value. ``WORKER_IMAGE=<image>``
    gives the tag of the image when the tag starts with a digit.
    """
    found: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        text = line.strip().lstrip("#").strip()
        name, sep, value = text.partition("=")
        value = value.strip().strip("\"'")
        if not sep or not value:
            continue
        if name == "JOSHUA_ADDONS_VERSION":
            found["JOSHUA_ADDONS_VERSION"] = value
        elif name == "WORKER_IMAGE":
            tag = _image_tag(value)
            if tag and tag[0].isdigit():
                found["WORKER_IMAGE tag"] = tag
    return found


def main() -> int:
    chart = yaml.safe_load(CHART.read_text())
    found = {
        "charts/joshua-addon/Chart.yaml version": str(chart.get("version", "")),
        "charts/joshua-addon/Chart.yaml appVersion": str(chart.get("appVersion", "")),
    }
    for compose in sorted(ADDONS_DIR.glob("*/docker-compose.yml")):
        name = compose.relative_to(ROOT).as_posix()
        found[f"{name} JOSHUA_ADDONS_VERSION default"] = _compose_default(compose)
        pinned, tag = _compose_worker_tag(compose)
        if pinned:
            found[f"{name} WORKER_IMAGE tag"] = tag
    ci_values = sorted((CHART.parent / "ci").glob("*-values.yaml"))
    for values in [*sorted(ADDONS_DIR.glob("*/values.yaml")), *ci_values]:
        pinned, tag = _values_worker_tag(values)
        if pinned:
            found[f"{values.relative_to(ROOT).as_posix()} env.WORKER_IMAGE tag"] = tag
    for env_file in sorted(ADDONS_DIR.glob("*/.env.example")):
        name = env_file.relative_to(ROOT).as_posix()
        for key, value in _env_example_versions(env_file).items():
            found[f"{name} {key}"] = value

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
    version = next(iter(found.values()))
    if not version_shape_ok(version):
        print(f"chart-version: '{version}' is not of the date shape.", file=sys.stderr)
        print(SHAPE_HELP, file=sys.stderr)
        return 1
    print(f"chart-version: clean ({version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
