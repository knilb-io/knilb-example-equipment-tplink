"""Domain error codes (SPEC §7.4) on top of the generic :class:`ServiceError`."""

from __future__ import annotations

from typing import Any, Literal

from equipment_tplink.core.models import PlugIdentity, Upstream
from equipment_tplink.kit.errors import ServiceError

STATUS: dict[str, int] = {
    "invalid_plug_ref": 400,
    "plug_not_found": 404,
    "confirmation_mismatch": 409,
    "mfa_not_pending": 409,
    "unsupported": 501,
    "device_unreachable": 502,
    "device_auth_failed": 502,
    "device_error": 502,
    "device_offline": 502,
    "cloud_error": 502,
    "cloud_not_configured": 503,
    "cloud_login_required": 503,
    "cloud_mfa_required": 503,
    "timeout": 504,
}

ErrorDriver = Literal["local", "cloud", "none"]


class PlugError(ServiceError):
    """An error about a plug or a driver."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        driver: ErrorDriver = "none",
        plug: PlugIdentity | None = None,
        upstream: Upstream | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code,
            message,
            status=STATUS[code],
            driver=driver,
            plug=plug,
            upstream=upstream,
            detail=detail,
        )

    def with_context(
        self, *, plug: PlugIdentity | None = None, upstream: Upstream | None = None
    ) -> PlugError:
        """Fill in ``plug`` and ``upstream`` if they are not set yet."""
        if plug is not None and self.fields.get("plug") is None:
            self.fields["plug"] = plug
        if upstream is not None and self.fields.get("upstream") is None:
            self.fields["upstream"] = upstream
        return self
