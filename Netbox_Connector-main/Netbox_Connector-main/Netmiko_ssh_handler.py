"""Legacy module forwarding to :mod:`netbox_connector.netmiko_ssh_handler`."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if _SRC_DIR.exists():
    sys.path.insert(0, str(_SRC_DIR))

from netbox_connector.netmiko_ssh_handler import *  # type: ignore[F401,F403]
from netbox_connector.netmiko_ssh_handler import __all__  # type: ignore[F401]
