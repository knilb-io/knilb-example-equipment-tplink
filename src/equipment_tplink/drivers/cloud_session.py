"""Cloud login state, driven through tplink-cloud-api's public API (SPEC §7.6).

``TPLinkDeviceManager.login`` blocks and asks for an MFA code through a synchronous
``mfa_callback`` (FINDINGS F5). The login runs in a worker thread; the callback waits
there until ``loginCloud`` supplies the code in a later HTTP request. Nothing is
persisted (F16): every start logs in again.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from tplinkcloud import TPLinkDeviceManager
from tplinkcloud.exceptions import TPLinkAuthError, TPLinkCloudError

from equipment_tplink.core.errors import PlugError
from equipment_tplink.core.models import (
    CloudSession,
    CloudState,
    ErrorSummary,
    Upstream,
)
from equipment_tplink.kit.clock import Clock
from equipment_tplink.kit.redact import mask_email, redact_text
from equipment_tplink.kit.upstream import Recorder, record, recording

ManagerFactory = Callable[..., Any]

MFA_WAIT_S = 300.0


class _MfaAbandoned(Exception):
    """Raised inside the MFA callback when no code arrives (or on logout)."""


class _NoToken(Exception):
    """The library finished a login without a token for either cloud."""


class _Attempt:
    """MFA hand-off state owned by one login attempt (shared with its thread)."""

    def __init__(self) -> None:
        self.prompted = asyncio.Event()
        self.code_ready = threading.Event()
        self.code: str | None = None
        self.abandoned = False

    def abandon(self) -> None:
        self.abandoned = True
        self.code_ready.set()


def default_term_id(username: str | None) -> str:
    """A terminal id that is stable per host and account (FINDINGS F6)."""
    name = f"equipment-tplink:{socket.gethostname()}:{username or ''}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name)).upper()


class CloudSessionManager:
    """Owns the ``TPLinkDeviceManager`` and the login state machine."""

    def __init__(
        self,
        *,
        username: str | None,
        password: str | None,
        clock: Clock,
        manager_factory: ManagerFactory | None = None,
        term_id: str | None = None,
        mfa_wait_s: float = MFA_WAIT_S,
    ) -> None:
        self._username = username
        self._password = password
        self._clock = clock
        self._factory: ManagerFactory = manager_factory or TPLinkDeviceManager
        self._term_id = term_id or default_term_id(username)
        self._mfa_wait_s = mfa_wait_s
        self.state = (
            CloudState.LOGIN_REQUIRED if self.configured else CloudState.NOT_CONFIGURED
        )
        self.last_error: ErrorSummary | None = None
        self.logged_in_at: datetime | None = None
        self.manager: Any = None
        self.generation = 0
        self._task: asyncio.Task[None] | None = None
        self._attempt: _Attempt | None = None
        self._upstream: Upstream | None = None

    @property
    def configured(self) -> bool:
        """Whether credentials are configured."""
        return bool(self._username and self._password)

    def snapshot(self) -> CloudSession:
        """The public view of the session."""
        return CloudSession(
            state=self.state,
            username_masked=mask_email(self._username),
            logged_in_at=self.logged_in_at,
            last_error=self.last_error,
        )

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Begin a background login at startup (never fatal)."""
        if self.configured:
            self._begin_login()

    async def close(self) -> None:
        """Release a login thread that may be waiting for an MFA code."""
        self._abandon_mfa()
        if self._task is not None and not self._task.done():
            await asyncio.wait({self._task}, timeout=1.0)

    # -- operations ----------------------------------------------------------------

    async def login(self, mfa_code: str | None, *, timeout: float) -> CloudSession:
        """Start a login, or hand the MFA code to the login waiting for it."""
        if not self.configured:
            raise self._error(
                "cloud_not_configured", "TPLINK_USERNAME/TPLINK_PASSWORD are not set"
            )
        if mfa_code is not None:
            if (
                self.state is not CloudState.MFA_REQUIRED
                or not self._waiting_for_code()
            ):
                raise PlugError(
                    "mfa_not_pending",
                    "no login is waiting for an MFA code; POST without mfa_code first",
                    driver="cloud",
                )
            assert self._attempt is not None
            self._attempt.code = mfa_code
            self._attempt.code_ready.set()
            await self._await_login(timeout, until_prompt=False)
            return self._result()
        if self._task is None or self._task.done():
            self._begin_login()
        await self._await_login(timeout, until_prompt=True)
        return self._result()

    async def logout(self) -> CloudSession:
        """Forget the login (and any pending MFA wait)."""
        self.generation += 1
        self._abandon_mfa()
        self.manager = None
        self.logged_in_at = None
        self.last_error = None
        self._upstream = None
        self.state = (
            CloudState.LOGIN_REQUIRED if self.configured else CloudState.NOT_CONFIGURED
        )
        return self.snapshot()

    def require_manager(self) -> Any:
        """Return the logged-in manager or raise the matching 503."""
        if self.state is CloudState.AUTHENTICATED and self.manager is not None:
            return self.manager
        if self.state is CloudState.NOT_CONFIGURED:
            raise self._error(
                "cloud_not_configured", "TPLINK_USERNAME/TPLINK_PASSWORD are not set"
            )
        if self.state is CloudState.MFA_REQUIRED:
            raise self._error(
                "cloud_mfa_required",
                "the cloud login is waiting for an MFA code; POST /v1/cloud/session "
                'with {"mfa_code": "…"}',
            )
        message = self.last_error.message if self.last_error else "not logged in"
        raise self._error("cloud_login_required", f"cloud login required: {message}")

    def mark_login_required(self, message: str) -> None:
        """Called when the library reports that the session is no longer valid."""
        self.generation += 1
        self.manager = None
        self.state = CloudState.LOGIN_REQUIRED
        self.last_error = ErrorSummary(code="cloud_login_required", message=message)

    # -- internals -----------------------------------------------------------------

    def _begin_login(self) -> None:
        self.generation += 1
        generation = self.generation
        loop = asyncio.get_running_loop()
        attempt = self._attempt = _Attempt()
        wait_s = self._mfa_wait_s
        self.state = CloudState.LOGIN_REQUIRED

        def on_prompt() -> None:
            if generation == self.generation:
                self.state = CloudState.MFA_REQUIRED
                self.last_error = None
            attempt.prompted.set()

        def mfa_callback(mfa_type: str, email: str) -> str:
            loop.call_soon_threadsafe(on_prompt)
            if not attempt.code_ready.wait(wait_s):
                raise _MfaAbandoned(f"no MFA code was supplied within {wait_s:.0f} s")
            if attempt.abandoned:
                raise _MfaAbandoned("the pending login was cancelled")
            return attempt.code or ""

        self._task = loop.create_task(self._run_login(generation, mfa_callback))

    async def _run_login(
        self, generation: int, mfa_callback: Callable[[str, str], str]
    ) -> None:
        recorder = Recorder(self._clock)

        def construct() -> Any:
            # The public constructor logs in to the Kasa cloud and, with
            # include_tapo, the Tapo cloud, where migrated accounts list Kasa plugs.
            return self._factory(
                username=self._username,
                password=self._password,
                prefetch=False,
                cache_devices=False,
                include_tapo=True,
                term_id=self._term_id,
                mfa_callback=mfa_callback,
            )

        with recording(recorder):
            try:
                async with record("login") as call:
                    manager = await asyncio.to_thread(construct)
                    call.response = {
                        "kasa_token": bool(manager.get_token()),
                        "tapo_token": bool(manager.get_tapo_token()),
                    }
                if not (manager.get_token() or manager.get_tapo_token()):
                    raise _NoToken(
                        "TP-Link returned no session token for the Kasa or Tapo "
                        "cloud; tplink-cloud-api ignores an MFA request nested in "
                        "the Kasa login reply (FINDINGS F23)"
                    )
            except (TPLinkAuthError, _MfaAbandoned, _NoToken) as exc:
                self._finish(generation, recorder, CloudState.LOGIN_REQUIRED, exc)
                return
            except TPLinkCloudError as exc:
                self._finish(generation, recorder, CloudState.ERROR, exc)
                return
            except Exception as exc:  # requests/network errors from the library
                self._finish(generation, recorder, CloudState.ERROR, exc)
                return
        if generation == self.generation:
            self.manager = manager
            self.state = CloudState.AUTHENTICATED
            self.logged_in_at = self._clock.now()
            self.last_error = None
            self._upstream = Upstream(driver="cloud", calls=recorder.calls)

    def _finish(
        self, generation: int, recorder: Recorder, state: CloudState, exc: BaseException
    ) -> None:
        if generation != self.generation:
            return
        code = (
            "cloud_login_required"
            if state is CloudState.LOGIN_REQUIRED
            else "cloud_error"
        )
        self.state = state
        self.manager = None
        self.last_error = ErrorSummary(
            code=code, message=redact_text(str(exc)) or type(exc).__name__
        )
        self._upstream = Upstream(driver="cloud", calls=recorder.calls)

    async def _await_login(self, timeout: float, *, until_prompt: bool) -> None:
        assert self._task is not None
        waiters: set[asyncio.Future[Any]] = {self._task}
        prompt_waiter = None
        if until_prompt and self._attempt is not None:
            prompt_waiter = asyncio.ensure_future(self._attempt.prompted.wait())
            waiters.add(prompt_waiter)
        try:
            done, _ = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if prompt_waiter is not None and not prompt_waiter.done():
                prompt_waiter.cancel()
        if not done:
            raise self._error(
                "timeout", f"the cloud login did not finish within {timeout:g} s"
            )

    def _result(self) -> CloudSession:
        if self.state is CloudState.AUTHENTICATED:
            return self.snapshot()
        if self.state is CloudState.MFA_REQUIRED:
            raise self._error(
                "cloud_mfa_required",
                "TP-Link sent a verification code; POST /v1/cloud/session with "
                '{"mfa_code": "…"} within '
                f"{self._mfa_wait_s:.0f} s",
            )
        message = self.last_error.message if self.last_error else "login failed"
        code = (
            "cloud_error" if self.state is CloudState.ERROR else "cloud_login_required"
        )
        raise self._error(code, message)

    def _waiting_for_code(self) -> bool:
        return (
            self._task is not None
            and not self._task.done()
            and self._attempt is not None
            and not self._attempt.code_ready.is_set()
        )

    def _abandon_mfa(self) -> None:
        if self._attempt is not None:
            self._attempt.abandon()

    def _error(self, code: str, message: str) -> PlugError:
        return PlugError(code, message, driver="cloud", upstream=self._upstream)
