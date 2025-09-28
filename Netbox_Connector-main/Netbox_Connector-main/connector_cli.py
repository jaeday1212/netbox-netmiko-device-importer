"""Legacy CLI entry point forwarding to :mod:`netbox_connector`."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if _SRC_DIR.exists():
    sys.path.insert(0, str(_SRC_DIR))

from netbox_connector.connector_cli import main  # type: ignore[F401]


__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
