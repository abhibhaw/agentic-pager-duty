"""Discovery by the Agent Production Control Plane ("autopilot").

The control plane lists this deployment in its Agents inventory because the
process *registers itself* with a workload credential and heartbeats. Nothing
is auto-detected: no monkey-patching, no zero-code instrumentation. All HTTP is
the ``acp_sdk`` package's; this module only decides *when* to call it.

## Telemetry never takes the agent down

Every entry point here swallows its own failures and logs a fixed message.
The exception text is never logged -- the SDK's messages can carry transport
detail -- only its type, which is enough to tell the three cases apart:

* ``PermissionError`` -- 401/403: the credential is bad, or it was not granted
  :data:`AGENT_KEY`. Fix the grant; do not loosen anything here.
* ``ValueError`` -- 422 or bad input (including ``ACP_ENDPOINT`` being plain
  http). The SDK refuses these at the call.
* ``ConnectionError`` -- the control plane was unreachable after retries.

In every case the agent keeps running, unobserved.

## Where each call happens

* **Registration** -- once per process, from the Starlette lifespan of the
  custom app in ``langgraph.json``. LangGraph Platform enters that lifespan in
  the API server *and* in each queue-worker process, which is where runs
  execute. Registration is the SDK's only blocking call (up to five attempts
  with backoff), and the platform warns at 10 s and fails readiness at 30 s of
  lifespan startup, so it runs on a daemon thread rather than in the lifespan.
* **Heartbeat** -- once after registering; the SDK then heartbeats every ~60 s
  on its own thread.
* **Runs** -- :func:`observe` attaches a callback to a compiled graph that
  opens one control-plane run per *platform* run. A run that parks on a gate's
  ``interrupt()`` ends normally (LangGraph reports it as a chain end, not an
  error), so it is recorded as ``succeeded``; the resume after approval is a
  new platform run and a new control-plane run.
* **Flush** -- from the lifespan's shutdown half, which is this deployment's
  container stop hook. The SDK also flushes at interpreter exit. No signal
  handlers are installed here: the platform's server owns SIGTERM, and
  replacing its handler would skip its own graceful shutdown.

## What is never sent

No prompt, tool argument, output, ticket id, subject or customer identifier.
The only run identifier is the platform's own run UUID. Tenant and environment
come from the credential server-side and are passed nowhere.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

#: Must equal the agent key granted on ``ACP_CREDENTIAL`` exactly, or
#: registration is refused with 403.
AGENT_KEY = "pagerduty-triage"
AGENT_NAME = "Pager-duty triage (Zoho -> sprint-tasks)"
FRAMEWORK = "langgraph"

ENV_ENDPOINT = "ACP_ENDPOINT"
ENV_CREDENTIAL = "ACP_CREDENTIAL"
#: Optional override for the per-replica key. Defaults to the hostname, which
#: is the pod name on Kubernetes.
ENV_INSTANCE_KEY = "ACP_INSTANCE_KEY"
#: Optional. The image digest or label of this release, reported on
#: heartbeats and runs. Falls back to the git SHA LangGraph Platform exposes.
ENV_RELEASE_DIGEST = "ACP_RELEASE_DIGEST"
_PLATFORM_REVISION = "LANGCHAIN_REVISION_ID"

FLUSH_TIMEOUT_S = 5.0

_lock = threading.Lock()
_started = False
_registered = threading.Event()


def instance_key() -> str:
    """Stable per replica: printable ASCII, no spaces, at most 256 chars."""
    raw = os.environ.get(ENV_INSTANCE_KEY, "").strip() or socket.gethostname()
    cleaned = "".join(c for c in raw if "!" <= c <= "~")[:256]
    return cleaned or "unknown-host"


def release_digest() -> str | None:
    for name in (ENV_RELEASE_DIGEST, _PLATFORM_REVISION):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def is_registered() -> bool:
    return _registered.is_set()


def register() -> bool:
    """Register this process and send the first heartbeat. Never raises.

    Blocking. Returns whether the process is now observed. Callers want
    :func:`start`, which runs this once per process on a background thread.
    """
    if not (os.environ.get(ENV_ENDPOINT) and os.environ.get(ENV_CREDENTIAL)):
        logger.info(
            "acp: %s/%s not set; running unobserved", ENV_ENDPOINT, ENV_CREDENTIAL
        )
        return False
    try:
        import acp_sdk

        key = instance_key()
        registration = acp_sdk.register_agent(
            agent_key=AGENT_KEY,
            name=AGENT_NAME,
            framework=FRAMEWORK,
            instance_key=key,
        )
    except (ConnectionError, PermissionError, ValueError) as exc:
        logger.warning(
            "acp: registration failed (%s); running unobserved", type(exc).__name__
        )
        return False
    except Exception as exc:  # noqa: BLE001 -- telemetry must never crash the app
        logger.warning(
            "acp: registration failed unexpectedly (%s); running unobserved",
            type(exc).__name__,
        )
        return False

    _registered.set()
    logger.info(
        "acp: registered workload_id=%s instance_key=%s agent_key=%s",
        registration.workload_id,
        key,
        AGENT_KEY,
    )
    try:
        acp_sdk.heartbeat(release_digest=release_digest())
    except ValueError:
        # A digest the contract rejects. Heartbeat without it rather than not
        # at all: liveness matters more than the release label.
        logger.warning("acp: release digest rejected; heartbeating without it")
        try:
            acp_sdk.heartbeat()
        except Exception:  # noqa: BLE001
            logger.warning("acp: first heartbeat failed")
    except Exception:  # noqa: BLE001
        logger.warning("acp: first heartbeat failed")
    return True


def start() -> None:
    """Register on a daemon thread, at most once per process. Returns at once."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=register, name="acp-register", daemon=True).start()


def flush() -> None:
    """Drain buffered run events and spans. Never raises.

    ``True`` from the SDK means only that the local buffer drained, not that
    anything was indexed server-side, so it is not reported as success.
    """
    if not is_registered():
        return
    try:
        import acp_sdk

        if not acp_sdk.flush(timeout_s=FLUSH_TIMEOUT_S):
            logger.warning("acp: flush timed out with events still buffered")
    except Exception:  # noqa: BLE001
        logger.warning("acp: flush failed")


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    """Starlette lifespan: register at startup, flush at shutdown."""
    start()
    try:
        yield
    finally:
        flush()


class RunObserver(BaseCallbackHandler):
    """Opens a control-plane run for each root graph run and closes it.

    Only root runs (``parent_run_id is None``) are observed; nodes, subagents
    and model calls are children and are ignored. The callback payloads --
    inputs, outputs, the exception message -- are never read.
    """

    #: ``start_run``/``finish_run`` only enqueue, so running inline on the
    #: event loop is safe, and it keeps start/finish ordered per run.
    run_inline = True
    raise_error = False

    def __init__(self) -> None:
        self._runs: dict[UUID, Any] = {}
        self._runs_lock = threading.Lock()

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id is not None or not is_registered():
            return
        try:
            import acp_sdk

            run = acp_sdk.start_run(external_run_id=str(run_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("acp: could not start run (%s)", type(exc).__name__)
            return
        with self._runs_lock:
            self._runs[run_id] = run

    def on_chain_end(
        self,
        outputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id is None:
            self._finish(run_id, "succeeded")

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id is None:
            cancelled = isinstance(error, asyncio.CancelledError)
            self._finish(run_id, "cancelled" if cancelled else "failed")

    def _finish(
        self, run_id: UUID, status: Literal["succeeded", "failed", "cancelled"]
    ) -> None:
        with self._runs_lock:
            run = self._runs.pop(run_id, None)
        if run is None:
            return
        try:
            import acp_sdk

            acp_sdk.finish_run(run, status=status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("acp: could not finish run (%s)", type(exc).__name__)


_observer = RunObserver()


def observe(graph: Any) -> Any:
    """Return ``graph`` with run reporting attached.

    Always attached, even before registration completes: the observer checks
    registration per run, so runs that start after the background thread
    registers are observed and earlier ones are simply not.
    """
    return graph.with_config(callbacks=[_observer])


__all__: list[str] = [
    "AGENT_KEY",
    "RunObserver",
    "flush",
    "instance_key",
    "is_registered",
    "lifespan",
    "observe",
    "register",
    "release_digest",
    "start",
]
