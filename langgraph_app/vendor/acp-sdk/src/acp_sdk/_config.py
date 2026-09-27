"""SDK configuration, limits and input validation.

Limits come from docs/05 (heartbeat every 60 s with jitter) and docs/06 (SDK buffers at most
10,000 span records / 20 MiB, dropping the oldest with counters; lifecycle drops counted
separately). Patterns mirror packages/contracts/openapi/api.json, so a value the server would
reject with 422 is refused at the call instead of being dropped later.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

ENV_ENDPOINT = "ACP_ENDPOINT"
ENV_CREDENTIAL = "ACP_CREDENTIAL"
# Full OTLP/HTTP traces URL (for example a Collector's `/v1/traces`). Unset: spans are not exported.
ENV_OTLP_TRACES_ENDPOINT = "ACP_OTLP_TRACES_ENDPOINT"
# Opt-in for plain http to a non-loopback host (for example a Compose service name).
ENV_ALLOW_INSECURE = "ACP_ALLOW_INSECURE_HTTP"

REGISTER_PATH = "/api/v1/workloads/register"
HEARTBEAT_PATH = "/api/v1/workloads/{workload_id}/heartbeat"
RUN_EVENTS_PATH = "/api/v1/run-events"

# WorkloadRegistrationRequest / WorkloadHeartbeatRequest.
TOKEN_PATTERN = re.compile(r"[\x21-\x7e]{1,256}")
# RunEventInput identifiers: external_run_id, parent_run_id, release_digest.
RUN_IDENTIFIER_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,255}")
MAX_TEXT = 256


@dataclass(frozen=True, slots=True)
class Limits:
    """Tunable bounds. Defaults are the documented ones; tests shrink them."""

    heartbeat_interval_s: float = 60.0
    heartbeat_jitter: float = 0.1  # +/- fraction of the interval
    heartbeat_attempts: int = 3
    register_attempts: int = 5
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 30.0
    retry_after_cap_s: float = 60.0
    request_timeout_s: float = 10.0
    lifecycle_max_events: int = 10_000
    run_events_batch: int = 100  # contract maxItems
    span_max_records: int = 10_000
    span_max_bytes: int = 20 * 1024 * 1024
    span_export_batch: int = 512
    shutdown_timeout_s: float = 5.0


@dataclass(frozen=True, slots=True)
class Config:
    endpoint: str
    credential: str
    otlp_traces_endpoint: str | None
    limits: Limits

    def __repr__(self) -> str:  # never show the credential
        return f"Config(endpoint={self.endpoint!r}, otlp={self.otlp_traces_endpoint!r})"


def resolve_config(
    *, endpoint: str | None, credential: str | None, limits: Limits | None = None
) -> Config:
    endpoint = endpoint or os.environ.get(ENV_ENDPOINT)
    credential = credential or os.environ.get(ENV_CREDENTIAL)
    if not endpoint:
        raise ValueError(f"no control-plane endpoint: pass endpoint= or set {ENV_ENDPOINT}")
    if not credential:
        raise ValueError(f"no credential: pass credential= or set {ENV_CREDENTIAL}")
    allow_insecure = os.environ.get(ENV_ALLOW_INSECURE, "").lower() in {"1", "true", "yes"}
    endpoint = _check_url(endpoint, allow_insecure=allow_insecure).rstrip("/")
    otlp = os.environ.get(ENV_OTLP_TRACES_ENDPOINT) or None
    if otlp is not None:
        otlp = _check_url(otlp, allow_insecure=allow_insecure)
    return Config(
        endpoint=endpoint,
        credential=credential,
        otlp_traces_endpoint=otlp,
        limits=limits or Limits(),
    )


def _check_url(url: str, *, allow_insecure: bool) -> str:
    """The bearer credential travels with every request, so plain http is only for loopback
    unless explicitly allowed. The URL itself is not echoed (it may embed userinfo)."""
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ValueError("endpoint must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ValueError("endpoint must not contain credentials")
    if parts.scheme == "http" and not allow_insecure and not _is_loopback(parts.hostname):
        raise ValueError(
            f"plain http is allowed only for loopback hosts; use https or set {ENV_ALLOW_INSECURE}"
        )
    return url


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_text(value: str, field: str) -> str:
    if not 1 <= len(value) <= MAX_TEXT:
        raise ValueError(f"{field} must be 1 to {MAX_TEXT} characters")
    return value


def check_token(value: str, field: str) -> str:
    if not TOKEN_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be 1 to 256 printable ASCII characters without spaces")
    return value


def check_run_identifier(value: str, field: str) -> str:
    if not RUN_IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field} must be an opaque ID: 1 to 256 ASCII letters, digits or ._:/- "
            "starting with a letter or digit (never customer text)"
        )
    return value
