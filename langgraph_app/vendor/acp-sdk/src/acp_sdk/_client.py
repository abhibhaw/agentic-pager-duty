"""The SDK client behind the public functions: registration, heartbeat, run lifecycle, flush.

Rules it follows (docs/05 Python SDK, docs/06 run lifecycle and failure behaviour):

- Requests carry only contract fields (packages/contracts/openapi/api.json). Tenant, environment
  and agent grants come from the bearer credential, so no request ever names them (invariant 1).
  No request carries prompts, tool arguments, outputs or exception messages (invariant 6).
- `register_agent` is the only synchronous call. `start_run`/`finish_run` only enqueue, and a
  daemon thread delivers run events in batches of at most 100. `heartbeat` runs on its own daemon
  thread every 60 s with jitter.
- Buffers are bounded and drop the oldest record with a counter; they never block the caller.
- A response the SDK cannot classify as a known result (transport error, timeout, 408, 429, 5xx,
  an unexpected status or a 2xx body that does not match the contract) is *unknown*: the request
  is retried with exponential backoff and full jitter, honouring `Retry-After`, and is never
  counted as delivered. Retrying is safe: registration is an idempotent upsert and run events
  are deduplicated by `event_id`, which a retry reuses.
- A known rejection (400, 401, 403, 404, 409, 413, 422) is not retried. The run-event batch is
  atomic on the server, so a batch rejected with 400/409/413/422 is resent one event at a time to
  isolate the bad one; 401/403/404 reject the whole batch (credential or workload, not an event).
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Final

import httpx
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import Span, StatusCode

from acp_sdk import _config
from acp_sdk._buffer import BoundedBuffer, backoff_delay, parse_retry_after
from acp_sdk._spans import BoundedSpanProcessor, inject_trace_context, make_tracer_provider
from acp_sdk._types import Registration, Run, RunTerminalStatus, _RunState

_log = logging.getLogger("acp_sdk")

_REJECTED_STATUSES: Final = frozenset({400, 401, 403, 404, 409, 413, 422})
# Rejections one event can cause; a batch rejected for these is resent one event at a time.
_EVENT_SPECIFIC_REJECTIONS: Final = frozenset({400, 409, 413, 422})
_ERROR_CODE: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_MAX_INT64: Final = 2**63 - 1
_USER_AGENT: Final = "acp-sdk/0.1.0"

Sleep = Callable[[float], bool]
"""Waits `seconds`; returns True when the caller should give up (the SDK is shutting down)."""


class _Outcome(Enum):
    OK = "ok"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class _Result:
    outcome: _Outcome
    status: int | None
    code: str | None = None
    retry_after_s: float | None = None
    body: Any = None


def _error_code(response: httpx.Response) -> str | None:
    """The envelope's stable code, if it looks like one; nothing else from the body is kept."""
    try:
        code = response.json()["error"]["code"]
    except Exception:
        return None
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else None


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_tokens(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_INT64:
        raise ValueError(f"{field} must be a nonnegative 64-bit integer")
    return value


class Client:
    def __init__(
        self,
        config: _config.Config,
        *,
        agent_key: str,
        name: str,
        framework: str,
        instance_key: str,
        transport: httpx.BaseTransport | None = None,
        span_exporter: SpanExporter | None = None,
        rng: random.Random | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Sleep | None = None,
        start_threads: bool = True,
    ) -> None:
        self._config = config
        self._limits = config.limits
        self._agent_key = _config.check_text(agent_key, "agent_key")
        self._name = _config.check_text(name, "name")
        self._framework = _config.check_text(framework, "framework")
        self._instance_key = _config.check_token(instance_key, "instance_key")
        self._rng = rng or random.Random()
        self._now = now or (lambda: datetime.now(UTC))
        self._abandon = threading.Event()  # set when shutdown stops waiting for delivery
        self._stop = threading.Event()
        self._sleep: Sleep = sleep or self._abandon.wait
        self._start_threads = start_threads
        self._http = httpx.Client(
            base_url=config.endpoint,
            transport=transport,
            timeout=self._limits.request_timeout_s,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {config.credential}",
                "User-Agent": _USER_AGENT,
            },
        )
        self._counters: dict[str, int] = {}
        self._counter_lock = threading.Lock()
        self._events: BoundedBuffer[dict[str, object]] = BoundedBuffer(
            max_items=self._limits.lifecycle_max_events
        )
        self._idle = threading.Condition()
        self._in_flight = 0
        self._release_digest: str | None = None
        self._heartbeat_now = threading.Event()
        self._threads: list[threading.Thread] = []
        self.registration: Registration | None = None
        self._closed = False

        if span_exporter is None and config.otlp_traces_endpoint is not None:
            span_exporter = OTLPSpanExporter(
                endpoint=config.otlp_traces_endpoint,
                headers={"Authorization": f"Bearer {config.credential}"},
                # Bounded by the shutdown budget so an unreachable Collector cannot outlast it.
                timeout=min(self._limits.request_timeout_s, self._limits.shutdown_timeout_s),
            )
        self._spans: BoundedSpanProcessor | None = None
        if span_exporter is not None:
            self._spans = BoundedSpanProcessor(
                span_exporter,
                max_records=self._limits.span_max_records,
                max_bytes=self._limits.span_max_bytes,
                batch_size=self._limits.span_export_batch,
                start_thread=start_threads,
            )
        self._provider: TracerProvider = make_tracer_provider(
            agent_key=self._agent_key,
            framework=self._framework,
            instance_key=self._instance_key,
            processors=[self._spans] if self._spans is not None else [],
        )
        self._tracer = self._provider.get_tracer("acp_sdk")

    # -- counters -----------------------------------------------------------------------------

    def _count(self, name: str, n: int = 1) -> None:
        if n:
            with self._counter_lock:
                self._counters[name] = self._counters.get(name, 0) + n

    def stats(self) -> dict[str, int]:
        with self._counter_lock:
            out = dict(self._counters)
        out["lifecycle_events_dropped"] = self._events.dropped
        out["lifecycle_events_buffered"] = len(self._events)
        if self._spans is not None:
            out["spans_dropped"] = self._spans.dropped
            out["spans_buffered"] = self._spans.buffered
            out["spans_exported"] = self._spans.exported
            out["spans_export_failed"] = self._spans.export_failed
        return out

    @property
    def span_export_configured(self) -> bool:
        return self._spans is not None

    # -- HTTP ---------------------------------------------------------------------------------

    def _post(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        ok_status: int,
        parse: Callable[[httpx.Response], Any] | None,
        with_trace: bool = False,
    ) -> _Result:
        headers: dict[str, str] = {}
        if with_trace:
            inject_trace_context(headers)
        try:
            response = self._http.post(path, json=body, headers=headers)
        except httpx.HTTPError as error:
            # The request may or may not have reached the server.
            _log.warning("acp_sdk: %s failed: %s", path, type(error).__name__)
            return _Result(_Outcome.UNKNOWN, None)
        status = response.status_code
        if status == ok_status:
            if parse is None:
                return _Result(_Outcome.OK, status)
            try:
                parsed = parse(response)
            except Exception:
                parsed = None
            if parsed is not None:
                return _Result(_Outcome.OK, status, body=parsed)
            _log.warning("acp_sdk: %s returned an unrecognised %d body", path, status)
            return _Result(_Outcome.UNKNOWN, status)
        if status in _REJECTED_STATUSES:
            return _Result(_Outcome.REJECTED, status, code=_error_code(response))
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        return _Result(
            _Outcome.UNKNOWN, status, code=_error_code(response), retry_after_s=retry_after
        )

    def _delay(self, attempt: int, result: _Result) -> float:
        return backoff_delay(
            attempt,
            base_s=self._limits.backoff_base_s,
            cap_s=self._limits.backoff_cap_s,
            rng=self._rng,
            retry_after_s=result.retry_after_s,
            retry_after_cap_s=self._limits.retry_after_cap_s,
        )

    # -- registration and heartbeat -----------------------------------------------------------

    def _parse_registration(self, response: httpx.Response) -> Registration | None:
        data = response.json()
        if not isinstance(data, dict):
            return None
        ids = [data.get(k) for k in ("agent_id", "workload_id", "environment_id")]
        instance_key = data.get("instance_key")
        if not all(_is_uuid(v) for v in ids) or instance_key != self._instance_key:
            return None
        agent_id, workload_id, environment_id = (str(v) for v in ids)
        return Registration(
            agent_id=agent_id,
            workload_id=workload_id,
            environment_id=environment_id,
            instance_key=self._instance_key,
        )

    def register(self) -> Registration:
        body = {
            "agent_key": self._agent_key,
            "name": self._name,
            "framework": self._framework,
            "instance_key": self._instance_key,
        }
        attempts = self._limits.register_attempts
        for attempt in range(attempts):
            result = self._post(
                _config.REGISTER_PATH,
                body,
                ok_status=200,
                parse=self._parse_registration,
                with_trace=True,
            )
            if result.outcome is _Outcome.OK:
                self.registration = result.body
                self._count("registrations")
                self._start_background()
                assert self.registration is not None
                return self.registration
            if result.outcome is _Outcome.REJECTED:
                code = result.code or "none"
                if result.status in {401, 403}:
                    raise PermissionError(
                        f"registration refused ({result.status}, code {code}): check the "
                        "credential's scopes and agent-key grant"
                    )
                raise ValueError(f"registration rejected ({result.status}, code {code})")
            self._count("registration_retries")
            if attempt + 1 < attempts and self._sleep(self._delay(attempt, result)):
                break
        raise ConnectionError(
            f"registration outcome unknown after {attempts} attempts; the control plane was "
            "unreachable or returned an unrecognised response"
        )

    def set_release_digest(self, release_digest: str | None) -> None:
        if release_digest is not None:
            self._release_digest = _config.check_token(release_digest, "release_digest")

    def request_heartbeat(self) -> None:
        self._heartbeat_now.set()

    def send_heartbeat(self) -> bool:
        """One heartbeat with bounded retries. Never raises; returns whether it was accepted."""
        registration = self.registration
        if registration is None:
            return False
        body: dict[str, object] = {"instance_key": self._instance_key}
        if self._release_digest is not None:
            body["release_digest"] = self._release_digest
        path = _config.HEARTBEAT_PATH.format(workload_id=registration.workload_id)
        attempts = self._limits.heartbeat_attempts
        for attempt in range(attempts):
            result = self._post(path, body, ok_status=204, parse=None)
            if result.outcome is _Outcome.OK:
                self._count("heartbeats_sent")
                return True
            if result.outcome is _Outcome.REJECTED:
                self._count("heartbeats_rejected")
                _log.warning(
                    "acp_sdk: heartbeat rejected (%s, code %s)", result.status, result.code
                )
                return False
            self._count("heartbeat_retries")
            if attempt + 1 < attempts and self._sleep(self._delay(attempt, result)):
                break
        self._count("heartbeats_failed")
        return False

    def _heartbeat_loop(self) -> None:
        limits = self._limits
        while not self._stop.is_set():
            jitter = self._rng.uniform(-limits.heartbeat_jitter, limits.heartbeat_jitter)
            self._heartbeat_now.wait(limits.heartbeat_interval_s * (1 + jitter))
            self._heartbeat_now.clear()
            if self._stop.is_set():
                return
            try:
                self.send_heartbeat()
            except Exception:  # never let the thread die silently on a bug
                _log.warning("acp_sdk: heartbeat failed unexpectedly", exc_info=False)

    # -- runs ---------------------------------------------------------------------------------

    def _enqueue(self, event: dict[str, object]) -> None:
        dropped = self._events.put(event)
        if dropped:
            _log.warning("acp_sdk: lifecycle buffer full; dropped %d oldest events", dropped)

    def _run_digest(self) -> str | None:
        digest = self._release_digest
        # The run-event contract is stricter than the heartbeat one; omit rather than be rejected.
        if digest is not None and _config.RUN_IDENTIFIER_PATTERN.fullmatch(digest):
            return digest
        return None

    def start_run(self, *, external_run_id: str | None, parent_run_id: str | None) -> Run:
        registration = self._require_registration()
        run_id = _config.check_run_identifier(
            external_run_id or str(uuid.uuid4()), "external_run_id"
        )
        if parent_run_id is not None:
            _config.check_run_identifier(parent_run_id, "parent_run_id")
            if parent_run_id == run_id:
                raise ValueError("parent_run_id must differ from external_run_id")
        digest = self._run_digest()
        started_at = self._now()
        attributes: dict[str, str] = {"acp.run.id": run_id}
        if parent_run_id is not None:
            attributes["acp.run.parent_id"] = parent_run_id
        if digest is not None:
            attributes["acp.release.digest"] = digest
        span = self._tracer.start_span("acp.run", attributes=attributes)
        run = Run(
            external_run_id=run_id,
            workload_id=registration.workload_id,
            parent_run_id=parent_run_id,
            started_at=started_at,
            _state=_RunState(self, span=span, release_digest=digest),
        )
        self._enqueue(self._event(run, "started", sequence=0, time=started_at))
        self._count("runs_started")
        return run

    def _event(self, run: Run, kind: str, *, sequence: int, time: datetime) -> dict[str, object]:
        event: dict[str, object] = {
            "event_id": str(uuid.uuid4()),
            "workload_id": run.workload_id,
            "external_run_id": run.external_run_id,
            "sequence": sequence,
            "type": kind,
            "time": time.astimezone(UTC).isoformat(),
        }
        if run.parent_run_id is not None:
            event["parent_run_id"] = run.parent_run_id
        if run._state is not None and run._state.release_digest is not None:
            event["release_digest"] = run._state.release_digest
        return event

    def finish_run(
        self,
        run: Run,
        *,
        status: RunTerminalStatus,
        business_succeeded: bool | None,
        input_tokens: int | None,
        output_tokens: int | None,
        error_type: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "cancelled", "timed_out"}:
            raise ValueError("status must be succeeded, failed, cancelled or timed_out")
        if business_succeeded is not None and not isinstance(business_succeeded, bool):
            raise ValueError("business_succeeded must be a bool or None")
        input_tokens = _check_tokens(input_tokens, "input_tokens")
        output_tokens = _check_tokens(output_tokens, "output_tokens")
        state = run._state
        if state is None or state.owner is not self:
            raise ValueError("run was not started by this SDK client")
        with state.lock:
            if state.finished:
                # One terminal event per run (docs/06); a second report is ignored, not sent.
                self._count("duplicate_finishes_ignored")
                return
            state.finished = True
        event = self._event(run, "finished", sequence=1, time=self._now())
        event["status"] = status
        if business_succeeded is not None:
            event["business_succeeded"] = business_succeeded
        if input_tokens is not None or output_tokens is not None:
            event["usage"] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        self._enqueue(event)
        self._count("runs_finished")
        span = state.span
        if isinstance(span, Span):
            if status in {"failed", "timed_out"}:
                span.set_status(StatusCode.ERROR)
                if error_type is not None:
                    span.set_attribute("error.type", error_type)
            elif status == "succeeded":
                span.set_status(StatusCode.OK)
            span.end()

    def _enter_run(self, run: Run) -> None:
        state = run._state
        if state is not None and isinstance(state.span, Span):
            state.context_token = otel_context.attach(trace.set_span_in_context(state.span))

    def _exit_run(self, run: Run, *, error_type: str | None) -> None:
        state = run._state
        if state is None:
            return
        try:
            if not state.finished:
                try:
                    self.finish_run(
                        run,
                        status="failed" if error_type is not None else "succeeded",
                        business_succeeded=None,
                        input_tokens=None,
                        output_tokens=None,
                        error_type=error_type,
                    )
                except Exception:  # never mask the application's own exception
                    _log.warning("acp_sdk: could not record the run finish", exc_info=False)
        finally:
            token = state.context_token
            if token is not None:
                state.context_token = None
                otel_context.detach(token)  # type: ignore[arg-type]

    def _require_registration(self) -> Registration:
        if self.registration is None:
            raise RuntimeError("call acp_sdk.register_agent() before starting runs")
        return self.registration

    # -- run-event delivery -------------------------------------------------------------------

    @staticmethod
    def _parse_accepted(response: httpx.Response) -> dict[str, int] | None:
        data = response.json()
        if (
            isinstance(data, dict)
            and _is_count(data.get("accepted"))
            and _is_count(data.get("duplicates"))
        ):
            return {"accepted": data["accepted"], "duplicates": data["duplicates"]}
        return None

    def deliver(self, batch: list[dict[str, object]]) -> None:
        """Deliver `batch` (1-100 events) until it is accepted, rejected or abandoned."""
        attempt = 0
        while True:
            result = self._post(
                _config.RUN_EVENTS_PATH,
                {"events": batch},
                ok_status=202,
                parse=self._parse_accepted,
            )
            if result.outcome is _Outcome.OK:
                self._count("lifecycle_events_sent", len(batch))
                self._count("lifecycle_events_duplicate", int(result.body["duplicates"]))
                return
            if result.outcome is _Outcome.REJECTED:
                if len(batch) > 1 and result.status in _EVENT_SPECIFIC_REJECTIONS:
                    # The server rolls back the whole batch; find the event it objects to.
                    self._count("lifecycle_batches_split")
                    for event in batch:
                        self.deliver([event])
                    return
                # 401/403/404 concern the credential or workload, not one event: no split.
                self._count("lifecycle_events_rejected", len(batch))
                _log.warning(
                    "acp_sdk: %d run events rejected (%s, code %s)",
                    len(batch),
                    result.status,
                    result.code,
                )
                return
            self._count("lifecycle_delivery_retries")
            if self._abandon.is_set() or self._sleep(self._delay(attempt, result)):
                self._count("lifecycle_events_unsent_at_shutdown", len(batch))
                return
            attempt += 1

    def drain_once(self) -> bool:
        """Deliver one batch from the buffer; returns False when the buffer was empty."""
        with self._idle:
            batch = self._events.take(self._limits.run_events_batch)
            self._in_flight = len(batch)
        if not batch:
            return False
        try:
            self.deliver(batch)
        finally:
            with self._idle:
                self._in_flight = 0
                self._idle.notify_all()
        return True

    def _sender_loop(self) -> None:
        while True:
            if self._events.wait_for_items(timeout_s=0.5, stop=self._stop):
                try:
                    self.drain_once()
                except Exception:
                    _log.warning("acp_sdk: run-event delivery failed unexpectedly", exc_info=False)
            elif self._stop.is_set():
                return
            if self._abandon.is_set():
                return

    # -- lifecycle ----------------------------------------------------------------------------

    def _start_background(self) -> None:
        if not self._start_threads or self._threads:
            return
        for target, name in (
            (self._sender_loop, "acp-sdk-run-events"),
            (self._heartbeat_loop, "acp-sdk-heartbeat"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def _idle_now(self) -> bool:
        return not len(self._events) and not self._in_flight

    def flush(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(timeout_s, 0.0)
        if self._threads:
            self._events.wake()
            with self._idle:
                while not self._idle_now():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._idle.wait(min(remaining, 0.05))
        else:
            while self._idle_now() is False:
                if time.monotonic() >= deadline:
                    return False
                self.drain_once()
        if self._spans is not None:
            remaining_ms = int(max(deadline - time.monotonic(), 0.0) * 1000)
            return self._spans.force_flush(remaining_ms)
        return True

    def shutdown(self, timeout_s: float) -> bool:
        """Flush within `timeout_s`, then stop; whatever is still buffered is counted as unsent."""
        if self._closed:
            return True
        self._closed = True
        flushed = self.flush(timeout_s)
        self._stop.set()
        self._abandon.set()
        self._heartbeat_now.set()
        self._events.wake()
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._count("lifecycle_events_unsent_at_shutdown", len(self._events.take(2**31)))
        self._provider.shutdown()
        self._http.close()
        return flushed
