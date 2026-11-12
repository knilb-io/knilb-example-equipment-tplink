"""Generic building blocks for FastAPI equipment services.

Nothing in this package may import TP-Link specific code (``core``, ``drivers``,
``fakes`` or ``routers``); a unit test enforces it. The next Knilb equipment service
can copy or extract this package as-is.
"""
