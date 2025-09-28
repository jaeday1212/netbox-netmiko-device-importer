"""Legacy datamodel shim forwarding to :mod:`netbox_connector.models`."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if _SRC_DIR.exists():
    sys.path.insert(0, str(_SRC_DIR))

from netbox_connector.models import *  # type: ignore[F401,F403]
from netbox_connector.models import __all__  # type: ignore[F401]
