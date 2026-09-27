"""Public SDK functions (docs/05-discovery-connectors.md#python-sdk-and-framework-adapter).

Signatures were frozen by L9-01; L9-02 aligned `finish_run` with the run-event contract
(`business_succeeded`, `input_tokens`, `output_tokens`) and delivered identity, lifecycle and
flush. L9-04/L9-05 deliver gateway actions and LangGraph pause/resume. Invariants callers can
rely on:

- Tenant, environment and agent scope are derived by the server from the credential; no function
  accepts them (CLAUDE.md invariant 1).
- `execute_tool` always goes through gateway execution and never accepts a local callable to run
  after an allow result.
- `idempotency_key` is required and caller-chosen. It must be stable across process restarts and
  graph replays, so it cannot be generated inside the call (see docs/status/l9.md, L9-01 spike).
- Protected action failures raise the typed exceptions in `acp_sdk.exceptions` (owned by L4):
  `ActionDenied`, `ApprovalPending(action_id)`, `DependencyUnavailable`,
  `UnknownOutcome(action_id)`. Telemetry failures never raise into the application; misuse
  (calling before `register_agent`, IDs the contract would reject) raises at the call.
- Callers persist action IDs from `ApprovalPending`/`UnknownOutcome` and resume with
  `resume_action`/`get_action`; an unknown outcome is never retried silently (invariant 5).
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Mapping

from acp_sdk import _config
from acp_sdk._client import Client
from acp_sdk._types import Action, JSONValue, Registration, Run, RunTerminalStatus

_log = logging.getLogger("acp_sdk")

# The process-wide client created by `register_agent`. Tests replace `_client_factory` to inject a
# fake transport; nothing else is configurable from outside.
_client: Client | None = None
_client_lock = threading.Lock()
_client_factory = Client
_atexit_registered = False


def _current() -> Client | None:
    return _client


def _stats() -> dict[str, int]:
    """Private delivery and drop counters (docs/06 failure behaviour), for diagnostics."""
    client = _client
    return client.stats() if client is not None else {}


def _shutdown(timeout_s: float | None = None) -> bool:
    """Flush and stop the current client. Registered with `atexit` by `register_agent`."""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is None:
        return True
    limit = timeout_s if timeout_s is not None else client._limits.shutdown_timeout_s
    try:
        return client.shutdown(limit)
    except Exception:
        _log.warning("acp_sdk: shutdown failed", exc_info=False)
        return False


def register_agent(
    *,
    agent_key: str,
    name: str,
    framework: str,
    instance_key: str,
    endpoint: str | None = None,
    credential: str | None = None,
) -> Registration:
    """Register (idempotently upsert) this workload instance: POST /api/v1/workloads/register.

    `endpoint` (the control-plane base URL) and `credential` (a workload credential with
    `workloads:register` and `telemetry:write`) default to the `ACP_ENDPOINT` and
    `ACP_CREDENTIAL` environment variables. The credential is never logged. Tenant, environment
    and agent grants come from the credential. Spans are exported only when
    `ACP_OTLP_TRACES_ENDPOINT` is set.

    Retries unknown outcomes (unreachable, 408/429/5xx, unrecognised response) with exponential
    backoff and full jitter, then raises `ConnectionError`. A refused credential raises
    `PermissionError` (401/403); a rejected request raises `ValueError` (for example 422, an
    invalid agent key). On success the SDK starts its heartbeat (every 60 s with jitter) and
    run-event delivery threads, and flushes on interpreter exit. Registering again replaces the
    previous registration after flushing it.
    """
    global _client, _atexit_registered
    config = _config.resolve_config(endpoint=endpoint, credential=credential)
    client = _client_factory(
        config,
        agent_key=agent_key,
        name=name,
        framework=framework,
        instance_key=instance_key,
    )
    try:
        registration = client.register()
    except BaseException:
        client.shutdown(0.0)
        raise
    _shutdown()
    with _client_lock:
        _client = client
        if not _atexit_registered:
            atexit.register(_shutdown)
            _atexit_registered = True
    return registration


def _require() -> Client:
    client = _client
    if client is None or client.registration is None:
        raise RuntimeError("call acp_sdk.register_agent() first")
    return client


def heartbeat(*, release_digest: str | None = None) -> None:
    """Report liveness and the observed release: POST /api/v1/workloads/{id}/heartbeat.

    Records `release_digest` (untrusted, never a deployment) for later heartbeats and runs, and
    asks the heartbeat thread to send now. Returns at once. Never raises on telemetry/transport
    failure; failures are counted instead. Raises `RuntimeError` before `register_agent` and
    `ValueError` for a digest the contract would reject.
    """
    client = _require()
    client.set_release_digest(release_digest)
    client.request_heartbeat()


def start_run(
    *,
    external_run_id: str | None = None,
    parent_run_id: str | None = None,
) -> Run:
    """Queue the unsampled `started` run event and return a run handle (a context manager).

    `external_run_id` defaults to a random UUID; both IDs must be opaque identifiers, never
    customer text. The run span continues the current W3C trace context. Never blocks on the
    network.
    """
    return _require().start_run(external_run_id=external_run_id, parent_run_id=parent_run_id)


def finish_run(
    run: Run,
    *,
    status: RunTerminalStatus,
    business_succeeded: bool | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Queue the one terminal `finished` event for `run`; delivery retries reuse its event ID.

    A second finish for the same run is ignored. Never blocks on the network.
    """
    _require().finish_run(
        run,
        status=status,
        business_succeeded=business_succeeded,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def execute_tool(
    *,
    tool_route_id: str,
    tool_schema_hash: str,
    arguments: Mapping[str, JSONValue],
    idempotency_key: str,
    run: Run | None = None,
) -> Action:
    """Request gateway execution of a tool route: POST /gateway/v1/actions (createGatewayAction).

    Returns the action once it has a known terminal outcome: state `succeeded`, or state `failed`
    with `execution.result == "known_failure"` and `execution.failure_code` (no exception exists
    for a known downstream failure; see docs/status/l9.md). Raises `ActionDenied`,
    `ApprovalPending(action_id)`, `DependencyUnavailable` (fails closed) or
    `UnknownOutcome(action_id)`, which is also raised for a state this SDK does not know.
    """
    raise NotImplementedError("execute_tool is delivered by L9-04")


def get_action(action_id: str) -> Action:
    """Read safe action status for resume: GET /gateway/v1/actions/{id} (getGatewayAction)."""
    raise NotImplementedError("get_action is delivered by L9-04")


def resume_action(action_id: str) -> Action:
    """Execute an approved, not-yet-started action: POST /gateway/v1/actions/{id}/execute
    (executeGatewayAction).

    Sends no new arguments. Returns and raises like `execute_tool`.
    """
    raise NotImplementedError("resume_action is delivered by L9-04")


def flush(*, timeout_s: float = 5.0) -> bool:
    """Deliver buffered run events and spans; returns False if the timeout elapsed first.

    Returns True when nothing is registered or buffered. Never raises on transport failure.
    """
    client = _client
    if client is None:
        return True
    try:
        return client.flush(timeout_s)
    except Exception:
        _log.warning("acp_sdk: flush failed", exc_info=False)
        return False
