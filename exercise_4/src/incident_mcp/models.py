"""Validated input/output models and the documented tool contract.

Every value the tools accept or return is described here. The constants in this
module are the single source of truth for defaults, bounds, allowlists, and
enumerations; the README repeats them for humans.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import ceil
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from .errors import InvalidArguments

# --- Time windows -----------------------------------------------------------

DEFAULT_WINDOW = timedelta(minutes=5)
MAX_LOG_WINDOW = timedelta(hours=24)
MAX_METRIC_WINDOW = timedelta(hours=24)
MAX_INCIDENT_WINDOW = timedelta(days=7)

# --- Result limits ----------------------------------------------------------

LOG_LIMIT_DEFAULT = 50
LOG_LIMIT_MAX = 200
INCIDENT_LIMIT_DEFAULT = 20
INCIDENT_LIMIT_MAX = 100
DEPLOY_LIMIT_DEFAULT = 5
DEPLOY_LIMIT_MAX = 50

# --- Size caps --------------------------------------------------------------

MAX_QUERY_CHARS = 200
MAX_MESSAGE_CHARS = 2_000
MAX_RUNBOOK_BYTES = 65_536
MAX_SUMMARY_CHARS = 5_000

SummaryText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SUMMARY_CHARS),
    Field(description="Incident analysis summary; trimmed, 1-5000 characters."),
]

# --- Buckets ----------------------------------------------------------------

MAX_BUCKETS = 500


class Bucket(StrEnum):
    """Supported latency aggregation widths."""

    S10 = "10s"
    S30 = "30s"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"

    @property
    def seconds(self) -> int:
        return BUCKET_SECONDS[self]


BUCKET_SECONDS: dict[Bucket, int] = {
    Bucket.S10: 10,
    Bucket.S30: 30,
    Bucket.M1: 60,
    Bucket.M5: 300,
    Bucket.M15: 900,
    Bucket.H1: 3_600,
}

BUCKET_DEFAULT = Bucket.M1

# --- Enumerations observed in the stand data --------------------------------

INCIDENT_ID_PATTERN = r"^INC-\d{3}$"


class Severity(StrEnum):
    """`incidents.severity`; enforced by a CHECK constraint in db/init.sql."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(StrEnum):
    """`incidents.status`; enforced by a CHECK constraint in db/init.sql."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class LogLevel(StrEnum):
    """`app_logs.level`.

    The stand has no CHECK constraint on this column. Observed values are
    ``INFO`` and ``WARN``; ``ERROR`` is accepted because it is the conventional
    third level and is present in the API's vocabulary.
    """

    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


# --- Service allowlists -----------------------------------------------------
#
# Service identifiers are exposed verbatim: no aliasing between the catalog
# names (`payments`) and the log/request names (`payments-api`).

CATALOG_SERVICES = frozenset({"payments", "checkout", "auth"})
LOG_SERVICES = frozenset({"payments-api", "checkout", "auth", "deploy-bot"})
METRIC_SERVICES = frozenset({"payments-api"})

METRIC_ENDPOINTS = frozenset(
    {
        "/api/v1/orders/{order_id}/price",
        "/api/v1/catalog/items",
    }
)


def require_service(value: str, allowed: frozenset[str], tool: str) -> str:
    """Validate a service identifier against a tool-specific allowlist."""

    if value not in allowed:
        raise InvalidArguments(
            f"unknown service for {tool}; valid values: {', '.join(sorted(allowed))}"
        )
    return value


def require_endpoint(value: str) -> str:
    if value not in METRIC_ENDPOINTS:
        raise InvalidArguments(
            "unknown endpoint for metrics_latency; valid values: "
            + ", ".join(sorted(METRIC_ENDPOINTS))
        )
    return value


def require_query(value: str | None) -> str | None:
    """Normalize a literal substring search term."""

    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_QUERY_CHARS:
        raise InvalidArguments(f"query is longer than {MAX_QUERY_CHARS} characters")
    return text


def require_summary(value: str) -> str:
    """Normalize and validate an incident summary before it is stored."""

    text = value.strip()
    if not text:
        raise InvalidArguments("summary must not be empty")
    if len(text) > MAX_SUMMARY_CHARS:
        raise InvalidArguments(f"summary is longer than {MAX_SUMMARY_CHARS} characters")
    return text


# --- Time range -------------------------------------------------------------


class TimeRange(BaseModel):
    """Half-open interval ``[start, end)`` with explicit timezone offsets."""

    model_config = ConfigDict(extra="forbid")

    start: AwareDatetime = Field(
        description="Inclusive lower bound, RFC 3339 with an explicit offset."
    )
    end: AwareDatetime = Field(
        description="Exclusive upper bound, RFC 3339 with an explicit offset."
    )

    @model_validator(mode="after")
    def _ordered(self) -> TimeRange:
        if self.start >= self.end:
            raise ValueError("start must be earlier than end")
        return self

    def utc(self) -> tuple[datetime, datetime]:
        return self.start.astimezone(UTC), self.end.astimezone(UTC)


class TimeRangeOut(BaseModel):
    """Resolved window actually queried, always UTC."""

    start: datetime
    end: datetime

    @classmethod
    def of(cls, start: datetime, end: datetime) -> TimeRangeOut:
        return cls(start=start.astimezone(UTC), end=end.astimezone(UTC))


def resolve_window(
    time_range: TimeRange | None,
    max_span: timedelta,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return ``[start, end)`` in UTC, defaulting to the last five minutes."""

    if time_range is None:
        end = (now or datetime.now(UTC)).astimezone(UTC)
        return end - DEFAULT_WINDOW, end

    start, end = time_range.utc()
    span = end - start
    if span > max_span:
        hours = max_span.total_seconds() / 3600
        raise InvalidArguments(
            f"time_range spans {span.total_seconds():.0f}s; the maximum allowed is "
            f"{max_span.total_seconds():.0f}s ({hours:g}h)"
        )
    return start, end


def bucket_count(start: datetime, end: datetime, bucket: Bucket) -> int:
    """Number of buckets the range would produce; bounded by MAX_BUCKETS."""

    span_seconds = (end - start).total_seconds()
    count = ceil(span_seconds / bucket.seconds)
    if count > MAX_BUCKETS:
        raise InvalidArguments(
            f"time_range with bucket={bucket.value} would produce {count} buckets; "
            f"the maximum is {MAX_BUCKETS}. Use a wider bucket."
        )
    return max(count, 1)


# --- Records ----------------------------------------------------------------


class Incident(BaseModel):
    id: str
    service: str
    severity: Severity
    status: IncidentStatus
    opened_at: datetime
    title: str
    summary: str | None = None


class Deploy(BaseModel):
    id: int
    service: str
    version: str
    deployed_at: datetime
    author: str
    notes: str


class LogEntry(BaseModel):
    ts: datetime
    service: str
    level: str
    message: str
    message_truncated: bool = False
    request_id: str | None = None


class LatencyBucket(BaseModel):
    bucket_start: datetime
    bucket_end: datetime
    request_count: int
    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    cache_hit_pct: float | None = None


class ServiceCatalogEntry(BaseModel):
    name: str
    team: str
    oncall: str
    dependencies: list[str]
    description: str


class Runbook(BaseModel):
    service: str
    path: str
    content: str
    size_bytes: int


class IncidentsResult(BaseModel):
    items: list[Incident]
    count: int
    limit: int
    has_more: bool
    service: str
    severity: Severity | None = None
    status: IncidentStatus | None = None
    time_range: TimeRangeOut | None = None


class IncidentResult(BaseModel):
    item: Incident


class IncidentAcknowledgeResult(BaseModel):
    item: Incident
    changed: bool
    previous_status: IncidentStatus


class IncidentSummaryResult(BaseModel):
    item: Incident
    changed: bool
    previous_summary_present: bool


class DeploysResult(BaseModel):
    items: list[Deploy]
    count: int
    limit: int
    has_more: bool
    service: str


class LogsResult(BaseModel):
    items: list[LogEntry]
    count: int
    limit: int
    has_more: bool
    service: str
    level: LogLevel | None = None
    query: str | None = None
    time_range: TimeRangeOut


class LatencyResult(BaseModel):
    service: str
    endpoint: str
    time_range: TimeRangeOut
    bucket: Bucket
    bucket_seconds: int
    total_requests: int
    buckets: list[LatencyBucket]


class RunbookResult(BaseModel):
    item: Runbook


class ServiceCatalogResult(BaseModel):
    item: ServiceCatalogEntry


__all__ = [
    "BUCKET_DEFAULT",
    "BUCKET_SECONDS",
    "CATALOG_SERVICES",
    "INCIDENT_ID_PATTERN",
    "INCIDENT_LIMIT_DEFAULT",
    "INCIDENT_LIMIT_MAX",
    "LOG_LIMIT_DEFAULT",
    "LOG_LIMIT_MAX",
    "LOG_SERVICES",
    "MAX_BUCKETS",
    "MAX_INCIDENT_WINDOW",
    "MAX_LOG_WINDOW",
    "MAX_MESSAGE_CHARS",
    "MAX_METRIC_WINDOW",
    "MAX_QUERY_CHARS",
    "MAX_RUNBOOK_BYTES",
    "MAX_SUMMARY_CHARS",
    "METRIC_ENDPOINTS",
    "METRIC_SERVICES",
    "Bucket",
    "Deploy",
    "DeploysResult",
    "Incident",
    "IncidentAcknowledgeResult",
    "IncidentResult",
    "IncidentStatus",
    "IncidentSummaryResult",
    "IncidentsResult",
    "LatencyBucket",
    "LatencyResult",
    "LogEntry",
    "LogLevel",
    "LogsResult",
    "Runbook",
    "RunbookResult",
    "ServiceCatalogEntry",
    "ServiceCatalogResult",
    "Severity",
    "SummaryText",
    "TimeRange",
    "TimeRangeOut",
    "bucket_count",
    "require_endpoint",
    "require_query",
    "require_service",
    "require_summary",
    "resolve_window",
]
