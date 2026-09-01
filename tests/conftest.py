"""Put the repository root on ``sys.path`` so a test can import a root script.

The root scripts live at ``scripts/`` with no ``pyproject.toml`` of their own,
so nothing installs them into the workspace venv. A test imports one as
``scripts.<name>``, which needs the repository root on the path.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
