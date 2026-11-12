"""REST service exposing TP-Link Kasa plugs to Knilb.

The package version lives only in ``pyproject.toml`` (python-semantic-release bumps
it there); it is read back from the installed distribution metadata at runtime.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("equipment-tplink")
except PackageNotFoundError:  # pragma: no cover - only when run from a bare checkout
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
