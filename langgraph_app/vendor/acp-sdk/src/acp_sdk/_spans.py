"""Span pipeline: an SDK-owned TracerProvider (never the global one), W3C trace-context
propagation without baggage, and a bounded span processor.

The OTel `BatchSpanProcessor` bounds only by count and drops the newest span; docs/06 asks for at
most 10,000 records / 20 MiB and dropping the oldest, so `BoundedSpanProcessor` does that.
`on_end` is O(1) and does no I/O. Before buffering, a span is reduced to allowlisted operational
attributes (invariant 6; server redaction stays authoritative, docs/09): unknown attributes, the
status description, event attributes other than `exception.type`, and link attributes are removed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Final

from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import AttributeValue

from acp_sdk._buffer import BoundedBuffer

_log = logging.getLogger("acp_sdk")

# SDK-set attributes (docs/06 internal mapping contract; `acp.*` is the product's private
# namespace) plus the sanitized error type. L9-05 extends this with pinned GenAI mappings.
SPAN_ATTRIBUTE_ALLOWLIST: Final = frozenset(
    {
        "acp.run.id",
        "acp.run.parent_id",
        "acp.release.digest",
        "error.type",
    }
)
_EVENT_ATTRIBUTE_ALLOWLIST: Final = frozenset({"exception.type"})

# Only `traceparent`/`tracestate`. The global propagator also carries baggage, whose values are
# off by default (docs/06).
_PROPAGATOR: Final = TraceContextTextMapPropagator()


def inject_trace_context(headers: MutableMapping[str, str], context: Context | None = None) -> None:
    _PROPAGATOR.inject(headers, context=context)


def _allowed(
    attributes: Mapping[str, AttributeValue] | None, allow: frozenset[str]
) -> dict[str, AttributeValue]:
    return {k: v for k, v in (attributes or {}).items() if k in allow}


def sanitize(span: ReadableSpan) -> ReadableSpan:
    events = [
        Event(e.name, _allowed(e.attributes, _EVENT_ATTRIBUTE_ALLOWLIST), timestamp=e.timestamp)
        for e in span.events
    ]
    links = [Link(link.context) for link in span.links]
    return ReadableSpan(
        name=span.name,
        context=span.get_span_context(),
        parent=span.parent,
        resource=span.resource,
        attributes=_allowed(span.attributes, SPAN_ATTRIBUTE_ALLOWLIST),
        events=events,
        links=links,
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def estimate_size(span: ReadableSpan) -> int:
    """A conservative in-memory estimate used for the byte bound (not an exact encoding)."""
    size = 256 + len(span.name)
    for key, value in (span.attributes or {}).items():
        size += 32 + len(key) + len(str(value))
    for event in span.events:
        size += 64 + len(event.name)
        for key, value in (event.attributes or {}).items():
            size += 32 + len(key) + len(str(value))
    size += 64 * len(span.links)
    return size


class BoundedSpanProcessor(SpanProcessor):
    """Buffers sanitized spans (drop-oldest, counted) and exports them from a daemon thread."""

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        max_records: int,
        max_bytes: int,
        batch_size: int,
        start_thread: bool = True,
    ) -> None:
        self._exporter = exporter
        self._buffer: BoundedBuffer[ReadableSpan] = BoundedBuffer(
            max_items=max_records, max_bytes=max_bytes, size_of=estimate_size
        )
        self._batch_size = batch_size
        self._lock = threading.Lock()  # one export at a time
        self._stop = threading.Event()
        self.exported = 0
        self.export_failed = 0
        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(target=self._run, name="acp-sdk-spans", daemon=True)
            self._thread.start()

    @property
    def dropped(self) -> int:
        return self._buffer.dropped

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def on_start(self, span: SdkSpan, parent_context: Context | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        try:
            if span.context is not None and not span.context.trace_flags.sampled:
                return
            self._buffer.put(sanitize(span))
        except Exception:  # telemetry never raises into the application
            _log.warning("acp_sdk: could not buffer a span", exc_info=False)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._buffer.wait_for_items(timeout_s=1.0, stop=self._stop):
                self._export_once()

    def _export_once(self) -> bool:
        with self._lock:
            batch = self._buffer.take(self._batch_size)
            if not batch:
                return True
            try:
                result = self._exporter.export(batch)
            except Exception:
                result = SpanExportResult.FAILURE
            if result is SpanExportResult.SUCCESS:
                self.exported += len(batch)
                return True
            # Spans are sampled telemetry: a failed export is counted, not retried forever.
            self.export_failed += len(batch)
            _log.warning("acp_sdk: span export failed; %d spans dropped", len(batch))
            return False

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Wait, within the deadline, until every buffered span has been handed to the exporter.

        With the worker thread running, the caller never calls the exporter itself: a slow or
        unreachable Collector cannot hold the caller past its deadline.
        """
        deadline = time.monotonic() + timeout_millis / 1000
        while len(self._buffer) or self._lock.locked():
            if time.monotonic() >= deadline:
                return False
            if self._thread is None:
                self._export_once()
            else:
                self._buffer.wake()
                time.sleep(min(0.01, max(deadline - time.monotonic(), 0.0)))
        return True

    def shutdown(self) -> None:
        self._stop.set()
        self._buffer.wake()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            self._exporter.shutdown()
        except Exception:
            _log.warning("acp_sdk: span exporter shutdown failed", exc_info=False)


def make_tracer_provider(
    *,
    agent_key: str,
    framework: str,
    instance_key: str,
    processors: Sequence[SpanProcessor],
) -> TracerProvider:
    """A provider private to the SDK; `trace.set_tracer_provider` is never called. The resource is
    built directly, not with `Resource.create`, so `OTEL_RESOURCE_ATTRIBUTES` cannot add
    unallowlisted attributes."""
    resource = Resource(
        {
            "service.name": agent_key,
            "service.instance.id": instance_key,
            "acp.agent.key": agent_key,
            "acp.framework": framework,
        }
    )
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    for processor in processors:
        provider.add_span_processor(processor)
    return provider
