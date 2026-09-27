"""Value types returned by the public SDK functions.

Names from docs/21-glossary-registries.md. `Action` and `ActionExecution` mirror the L4 contract
in packages/contracts/openapi/gateway.json; tests/test_contract_shapes.py fails when they drift
(docs/20-code-conventions.md#python-sdk-and-connectors). `Registration` mirrors
`WorkloadRegistration` in packages/contracts/openapi/api.json.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, TypeAlias, cast, get_args

from acp_sdk.exceptions import UnknownOutcome

# docs/21 "Run status" minus `running` (the SDK reports it with start_run) and `unknown`
# (derived by the server when no finish arrives, docs/06 run lifecycle).
RunTerminalStatus = Literal["succeeded", "failed", "cancelled", "timed_out"]

# docs/21 "Action state". `received` is internal and never returned. Clients reject unknown
# states conservatively (docs/04 version evolution), so this Literal is closed on purpose.
ActionState = Literal[
    "denied",
    "pending_approval",
    "approved",
    "ready",
    "dispatching",
    "succeeded",
    "failed",
    "unknown_outcome",
    "rejected",
    "expired",
    "cancelled",
    "invalidated",
]

# docs/21 "Decision".
Decision = Literal["allow", "deny", "require_approval"]

JSONValue: TypeAlias = str | int | float | bool | list["JSONValue"] | dict[str, "JSONValue"] | None


@dataclass(frozen=True, slots=True)
class Registration:
    """Result of `register_agent`; mirrors `WorkloadRegistration` in
    packages/contracts/openapi/api.json. Tenant and environment come from the credential."""

    agent_id: str
    workload_id: str
    environment_id: str
    instance_key: str


class _RunOwner(Protocol):
    """What a `Run` calls back into: the SDK client that started it (acp_sdk._client)."""

    def _enter_run(self, run: Run) -> None: ...

    def _exit_run(self, run: Run, *, error_type: str | None) -> None: ...


class _RunState:
    """Mutable per-run SDK state behind the frozen `Run` value: whether the one terminal event
    was queued, the run span, and the release digest captured at start (one per run, so start
    and finish never report conflicting digests)."""

    __slots__ = ("context_token", "finished", "lock", "owner", "release_digest", "span")

    def __init__(
        self, owner: _RunOwner, *, span: object | None, release_digest: str | None
    ) -> None:
        self.owner = owner
        self.span = span
        self.release_digest = release_digest
        self.context_token: object | None = None
        self.finished = False
        self.lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class Run:
    """Handle for one run started with `start_run`.

    As a context manager it makes the run's span current (so SDK calls inside carry its W3C trace
    context) and, on exit, queues the terminal event: `succeeded` without an exception, `failed`
    with one. The exception propagates and its message is never recorded (docs/06). Call
    `finish_run` inside the block to report another status; the exit then adds nothing.
    """

    external_run_id: str
    workload_id: str
    parent_run_id: str | None
    started_at: datetime
    _state: _RunState | None = field(default=None, compare=False, repr=False)

    def __enter__(self) -> Run:
        if self._state is not None:
            self._state.owner._enter_run(self)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._state is not None:
            # Only the exception's type name is reported (docs/06: sanitized type, no message).
            error_type = exc_type.__name__ if isinstance(exc_type, type) else None
            self._state.owner._exit_run(self, error_type=error_type)


# docs/21 "Execution result".
ExecutionResultKind = Literal["success", "known_failure", "unknown_outcome"]

_ACTION_STATES: frozenset[str] = frozenset(get_args(ActionState))


@dataclass(frozen=True, slots=True)
class ActionExecution:
    """Mirrors `GatewayActionExecution` in packages/contracts/openapi/gateway.json."""

    result: ExecutionResultKind
    # Schema-selected safe tool result, when the route returns one; never raw output.
    output: dict[str, JSONValue] | None
    failure_code: str | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class Action:
    """Mirrors `GatewayAction` in packages/contracts/openapi/gateway.json (operations
    createGatewayAction, getGatewayAction, executeGatewayAction, cancelGatewayAction).

    Safe status only: never the submitted arguments (invariant 6). `decision` is the initial
    policy decision and never means the tool ran.
    """

    id: str
    workload_id: str
    tool_route_id: str
    state: ActionState
    decision: Decision
    reason_codes: tuple[str, ...]
    policy_version_id: str | None
    approval_id: str | None
    expires_at: datetime
    created_at: datetime
    revision: int
    poll_after_ms: int | None
    execution: ActionExecution | None


def _parse_action_state(value: str, *, action_id: str) -> ActionState:
    """Accept only the known action states (docs/04 version evolution).

    An unknown state is treated as the most conservative known one: the outcome is unknown, so
    the caller must not retry it as a new action and should look it up later (invariant 5).
    """
    if value not in _ACTION_STATES:
        raise UnknownOutcome(action_id)
    return cast("ActionState", value)
