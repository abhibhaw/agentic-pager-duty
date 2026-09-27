"""Typed closed failures of protected actions (docs/05-discovery-connectors.md, Python SDK).

`execute_tool`, `get_action` and `resume_action` raise these instead of returning an ambiguous
value, so an application never mistakes "not executed" or "not known" for success:

- `ActionDenied`: action state `denied`, `rejected`, `expired`, `cancelled` or `invalidated`.
- `ApprovalPending`: state `pending_approval`, or `approved` and not resumed yet.
- `UnknownOutcome`: state `unknown_outcome`.
- `DependencyUnavailable`: 503 `DEPENDENCY_UNAVAILABLE` or 429 from the gateway, or no answer
  at all, so the gateway may not have decided.

Persist `action_id` across process restarts and resume with it; never submit a new action for
an uncertain one (CLAUDE.md invariant 5). An `invalidated` action needs a new request with a new
idempotency key. Messages carry only the action ID, states and codes, never tool arguments,
results or credentials.

These four names are the public exception API; the shared base class is private.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

__all__ = ["ActionDenied", "ApprovalPending", "DependencyUnavailable", "UnknownOutcome"]


class _ProtectedActionError(Exception):
    """Base of the SDK's typed action failures. `action_id` is None only when the gateway never
    returned one (the request may not have reached it)."""

    def __init__(self, message: str, *, action_id: str | None) -> None:
        self.action_id = action_id
        super().__init__(message)


class ActionDenied(_ProtectedActionError):  # noqa: N818 - public name fixed by docs/05
    """The action did not run and never will: denied by policy, rejected or not approved in time,
    cancelled, or invalidated by a policy, route or schema change."""

    def __init__(self, action_id: str, *, state: str, reason_codes: Iterable[str] = ()) -> None:
        self.state = state
        self.reason_codes: tuple[str, ...] = tuple(reason_codes)
        codes = ", ".join(self.reason_codes) or "none"
        super().__init__(
            f"action {action_id} was not executed (state {state}; reason codes: {codes})",
            action_id=action_id,
        )


class ApprovalPending(_ProtectedActionError):  # noqa: N818 - public name fixed by docs/05
    """The action waits for a human decision, or is approved and waits for `resume_action`.

    Nothing executes on its own: keep `action_id` and resume it with bounded polling
    (`poll_after_ms`) until `expires_at`.
    """

    def __init__(
        self,
        action_id: str,
        *,
        approval_id: str | None = None,
        expires_at: datetime | None = None,
        poll_after_ms: int | None = None,
    ) -> None:
        self.approval_id = approval_id
        self.expires_at = expires_at
        self.poll_after_ms = poll_after_ms
        super().__init__(f"action {action_id} is waiting for approval", action_id=action_id)


class DependencyUnavailable(_ProtectedActionError):  # noqa: N818 - public name fixed by docs/05
    """The gateway could not be reached, or it or one of its dependencies could not decide.

    Retry the same request with the same idempotency key after `retry_after_seconds`, or look the
    action up by `action_id` when one was returned. One key never becomes two actions (docs/04).
    """

    def __init__(
        self,
        *,
        action_id: str | None = None,
        code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        subject = f"action {action_id}" if action_id is not None else "the action request"
        super().__init__(
            f"{subject} could not be processed: {code or 'dependency unavailable'}",
            action_id=action_id,
        )


class UnknownOutcome(_ProtectedActionError):  # noqa: N818 - public name fixed by docs/05
    """The tool may or may not have had its side effect. Do not retry it as a new action; an
    operator reconciles it, and `get_action` reports the resolved state later."""

    def __init__(self, action_id: str) -> None:
        super().__init__(f"action {action_id} has an unknown outcome", action_id=action_id)
