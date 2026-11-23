"""Contract tests for the seven MCP tools, exercised through a real client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from oncall_mcp.runtime import StandRuntime
from tests.conftest import CATALOG_ENDPOINT, FIXED_END, FIXED_START, INJECTION_TEXT

EXPECTED_TOOLS = {
    "incidents_search",
    "incident_get",
    "deploys_recent",
    "logs_query",
    "metrics_latency",
    "runbook_get",
    "service_catalog_get",
}


def window(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


async def call(client: Client, name: str, arguments: dict) -> dict:
    result = await client.call_tool(name, arguments)
    assert result.structured_content is not None
    return result.structured_content


# --- discovery --------------------------------------------------------------


async def test_all_seven_tools_are_discoverable_with_titles(client: Client) -> None:
    tools = await client.list_tools()
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == EXPECTED_TOOLS
    for name, tool in by_name.items():
        assert tool.title, f"{name} has no title"
        assert tool.description and len(tool.description) > 80, f"{name} description too thin"
        assert "read-only" in tool.description.lower(), f"{name} does not state read-only"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True, f"{name} not marked readOnlyHint"
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
        assert schema and schema.get("type") == "object", f"{name} has no input schema"


async def test_tool_outputs_have_schemas(client: Client) -> None:
    tools = {tool.name: tool for tool in await client.list_tools()}
    for name in ("incidents_search", "metrics_latency", "runbook_get"):
        schema = getattr(tools[name], "output_schema", None) or getattr(
            tools[name], "outputSchema", None
        )
        assert schema, f"{name} has no output schema"


# --- incidents_search -------------------------------------------------------


async def test_incidents_search_orders_newest_first(client: Client) -> None:
    payload = await call(client, "incidents_search", {"service": "payments"})

    assert [item["id"] for item in payload["items"]] == ["INC-001", "INC-004"]
    assert payload["count"] == 2
    assert payload["has_more"] is False
    opened = [item["opened_at"] for item in payload["items"]]
    assert opened == sorted(opened, reverse=True)
    assert payload["time_range"] is None


async def test_incidents_search_filters_severity_and_status(client: Client) -> None:
    critical = await call(
        client, "incidents_search", {"service": "payments", "severity": "critical"}
    )
    assert [item["id"] for item in critical["items"]] == ["INC-004"]
    assert critical["severity"] == "critical"

    resolved = await call(client, "incidents_search", {"service": "auth", "status": "resolved"})
    assert [item["id"] for item in resolved["items"]] == ["INC-003"]
    assert resolved["status"] == "resolved"


async def test_incidents_search_filters_time_range(client: Client) -> None:
    payload = await call(
        client,
        "incidents_search",
        {
            "service": "payments",
            "time_range": window(
                datetime(2026, 7, 26, tzinfo=UTC), datetime(2026, 7, 28, tzinfo=UTC)
            ),
        },
    )
    assert [item["id"] for item in payload["items"]] == ["INC-004"]
    assert payload["time_range"]["start"] == "2026-07-26T00:00:00Z"
    assert payload["time_range"]["end"] == "2026-07-28T00:00:00Z"


async def test_incidents_search_no_match_returns_empty_list(client: Client) -> None:
    payload = await call(client, "incidents_search", {"service": "auth", "severity": "critical"})
    assert payload["items"] == []
    assert payload["count"] == 0
    assert payload["has_more"] is False


async def test_incidents_search_limit_and_has_more(client: Client) -> None:
    payload = await call(client, "incidents_search", {"service": "payments", "limit": 1})
    assert payload["count"] == 1
    assert payload["has_more"] is True
    assert payload["limit"] == 1


async def test_incidents_search_rejects_unknown_service(client: Client) -> None:
    with pytest.raises(ToolError, match="invalid_arguments"):
        await client.call_tool("incidents_search", {"service": "payments-api"})


async def test_incidents_search_rejects_oversized_range(client: Client) -> None:
    with pytest.raises(ToolError, match="maximum allowed"):
        await client.call_tool(
            "incidents_search",
            {
                "service": "payments",
                "time_range": window(FIXED_START - timedelta(days=8), FIXED_END),
            },
        )


# --- incident_get -----------------------------------------------------------


async def test_incident_get_returns_record(client: Client) -> None:
    payload = await call(client, "incident_get", {"incident_id": "INC-001"})
    item = payload["item"]
    assert item["id"] == "INC-001"
    assert item["service"] == "payments"
    assert item["severity"] == "high"
    assert item["status"] == "open"
    assert item["opened_at"].endswith("Z")


async def test_incident_get_missing_id_is_not_found(client: Client) -> None:
    with pytest.raises(ToolError, match="not_found"):
        await client.call_tool("incident_get", {"incident_id": "INC-999"})


@pytest.mark.parametrize(
    "bad_id", ["INC-1", "inc-001", "1 OR 1=1", "INC-001; DROP TABLE incidents"]
)
async def test_incident_get_rejects_malformed_ids(client: Client, bad_id: str) -> None:
    with pytest.raises(ToolError):
        await client.call_tool("incident_get", {"incident_id": bad_id})


# --- deploys_recent ---------------------------------------------------------


async def test_deploys_recent_descending_with_default_limit(client: Client) -> None:
    payload = await call(client, "deploys_recent", {"service": "payments"})
    assert [item["version"] for item in payload["items"]] == [
        "v1.5.0",
        "v1.4.2",
        "v1.4.1",
    ]
    assert payload["limit"] == 5
    deployed = [item["deployed_at"] for item in payload["items"]]
    assert deployed == sorted(deployed, reverse=True)


async def test_deploys_recent_limit_and_has_more(client: Client) -> None:
    payload = await call(client, "deploys_recent", {"service": "payments", "limit": 1})
    assert payload["count"] == 1
    assert payload["has_more"] is True
    assert payload["items"][0]["version"] == "v1.5.0"


@pytest.mark.parametrize(
    ("arguments", "match"),
    [
        ({"service": "payments-api"}, "invalid_arguments"),
        ({"service": "payments", "limit": 0}, None),
        ({"service": "payments", "limit": 51}, None),
    ],
)
async def test_deploys_recent_rejects_invalid_input(
    client: Client, arguments: dict, match: str | None
) -> None:
    with pytest.raises(ToolError, match=match):
        await client.call_tool("deploys_recent", arguments)


# --- logs_query -------------------------------------------------------------


async def test_logs_query_filters_level(client: Client) -> None:
    payload = await call(
        client,
        "logs_query",
        {
            "service": "payments-api",
            "time_range": window(FIXED_START, FIXED_END),
            "level": "ERROR",
        },
    )
    assert payload["count"] == 1
    assert payload["items"][0]["level"] == "ERROR"
    assert "timeout calling pricing" in payload["items"][0]["message"]


async def test_logs_query_substring_is_literal_not_regex(client: Client) -> None:
    payload = await call(
        client,
        "logs_query",
        {
            "service": "payments-api",
            "time_range": window(FIXED_START, FIXED_END),
            "query": "100%",
        },
    )
    assert payload["count"] == 1
    assert "literal 100% match" in payload["items"][0]["message"]

    underscore = await call(
        client,
        "logs_query",
        {
            "service": "payments-api",
            "time_range": window(FIXED_START, FIXED_END),
            "query": "a_b",
        },
    )
    assert underscore["count"] == 1


async def test_logs_query_returns_injection_text_as_data(client: Client) -> None:
    payload = await call(
        client,
        "logs_query",
        {
            "service": "checkout",
            "time_range": window(FIXED_START, FIXED_END),
            "query": "Ignore previous instructions",
        },
    )
    assert payload["count"] == 1
    assert payload["items"][0]["message"] == INJECTION_TEXT


async def test_logs_query_defaults_to_last_five_minutes(client: Client) -> None:
    payload = await call(client, "logs_query", {"service": "payments-api"})

    assert payload["count"] >= 1
    assert payload["time_range"]["end"] >= payload["time_range"]["start"]
    span = datetime.fromisoformat(payload["time_range"]["end"]) - datetime.fromisoformat(
        payload["time_range"]["start"]
    )
    assert span == timedelta(minutes=5)
    assert all("old startup line" not in item["message"] for item in payload["items"])
    timestamps = [item["ts"] for item in payload["items"]]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_logs_query_has_more_and_message_cap(client: Client) -> None:
    payload = await call(client, "logs_query", {"service": "payments-api", "limit": 1})
    assert payload["count"] == 1
    assert payload["has_more"] is True
    assert payload["items"][0]["message_truncated"] is False


async def test_logs_query_truncates_oversized_messages(client: Client) -> None:
    payload = await call(
        client,
        "logs_query",
        {
            "service": "payments-api",
            "time_range": window(FIXED_START, FIXED_END),
            "query": "long message",
        },
    )
    assert payload["count"] == 1
    entry = payload["items"][0]
    assert entry["message_truncated"] is True
    assert len(entry["message"]) == 2000


async def test_logs_query_accepts_a_log_only_service(client: Client) -> None:
    payload = await call(
        client,
        "logs_query",
        {
            "service": "deploy-bot",
            "time_range": window(datetime(2026, 7, 28, 9, 35, tzinfo=UTC), FIXED_END),
        },
    )
    assert payload["count"] == 1
    assert payload["items"][0]["service"] == "deploy-bot"


@pytest.mark.parametrize(
    ("arguments", "match"),
    [
        ({"service": "payments"}, "invalid_arguments"),
        ({"service": "payments-api", "level": "DEBUG"}, None),
        ({"service": "payments-api", "query": "x" * 201}, None),
        (
            {
                "service": "payments-api",
                "time_range": window(FIXED_START - timedelta(days=2), FIXED_END),
            },
            "maximum allowed",
        ),
        (
            {
                "service": "payments-api",
                "time_range": {"start": "2026-07-28T09:45:00Z", "end": "2026-07-28T09:40:00Z"},
            },
            None,
        ),
    ],
)
async def test_logs_query_rejects_invalid_input(
    client: Client, arguments: dict, match: str | None
) -> None:
    with pytest.raises(ToolError, match=match):
        await client.call_tool("logs_query", arguments)


# --- runbook_get ------------------------------------------------------------


async def test_runbook_get_returns_markdown(client: Client) -> None:
    payload = await call(client, "runbook_get", {"service": "payments"})
    item = payload["item"]
    assert item["service"] == "payments"
    assert item["path"] == "payments.md"
    assert "Runbook: payments" in item["content"]
    assert item["size_bytes"] == len(item["content"].encode("utf-8"))


@pytest.mark.parametrize(
    "service", ["payments-api", "deploy-bot", "../payments", "/etc/passwd", "payments.md"]
)
async def test_runbook_get_rejects_unknown_or_traversing_service(
    client: Client, service: str
) -> None:
    with pytest.raises(ToolError, match="invalid_arguments"):
        await client.call_tool("runbook_get", {"service": service})


# --- service_catalog_get ----------------------------------------------------


async def test_service_catalog_get_returns_ownership(client: Client) -> None:
    payload = await call(client, "service_catalog_get", {"service": "payments"})
    item = payload["item"]
    assert item["team"] == "billing"
    assert item["oncall"] == "a.kuznetsov"
    assert item["dependencies"] == ["postgres-main", "checkout", "fraud-scoring"]


async def test_service_catalog_get_rejects_unknown_service(client: Client) -> None:
    with pytest.raises(ToolError, match="invalid_arguments"):
        await client.call_tool("service_catalog_get", {"service": "billing"})


# --- metrics_latency: input contract ---------------------------------------


async def test_metrics_rejects_unknown_endpoint(client: Client) -> None:
    with pytest.raises(ToolError, match="invalid_arguments"):
        await client.call_tool(
            "metrics_latency",
            {"service": "payments-api", "endpoint": "/health"},
        )


async def test_metrics_rejects_unknown_service(client: Client) -> None:
    with pytest.raises(ToolError, match="invalid_arguments"):
        await client.call_tool(
            "metrics_latency", {"service": "checkout", "endpoint": CATALOG_ENDPOINT}
        )


async def test_metrics_rejects_invalid_bucket(client: Client) -> None:
    with pytest.raises(ToolError):
        await client.call_tool(
            "metrics_latency",
            {
                "service": "payments-api",
                "endpoint": CATALOG_ENDPOINT,
                "bucket": "7m",
            },
        )


# --- error hygiene ----------------------------------------------------------


async def test_error_messages_do_not_leak_internals(client: Client) -> None:
    errors: list[str] = []
    for name, arguments in [
        ("incidents_search", {"service": "nope"}),
        ("incident_get", {"incident_id": "INC-999"}),
        ("logs_query", {"service": "nope"}),
        ("runbook_get", {"service": "nope"}),
    ]:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(name, arguments)
        errors.append(str(caught.value))

    for message in errors:
        lowered = message.lower()
        for forbidden in ("postgresql://", "password", "traceback", "select ", "psycopg"):
            assert forbidden not in lowered, f"leaked {forbidden!r} in {message!r}"


async def test_tool_reports_unavailable_runtime(runtime: StandRuntime, client: Client) -> None:
    # Close the pool under a live session: every later tool call must fail with
    # a controlled data_source_unavailable error, not a traceback.
    await runtime.stop()

    with pytest.raises(ToolError, match="data_source_unavailable"):
        await client.call_tool("service_catalog_get", {"service": "payments"})
