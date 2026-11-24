"""Contract tests for the two authorized write tools."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from incident_mcp import repositories
from incident_mcp.runtime import StandRuntime


async def call(client: Client, name: str, arguments: dict) -> dict:
    result = await client.call_tool(name, arguments)
    assert result.structured_content is not None
    return result.structured_content


def incident_row(dsn: str, incident_id: str) -> tuple:
    with psycopg.connect(dsn) as conn:
        cur = conn.execute(
            "SELECT id, service, severity, status, opened_at, title, summary "
            "FROM incidents WHERE id = %s",
            (incident_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return row


# --- incident_acknowledge ---------------------------------------------------


async def test_acknowledge_open_incident(
    client: Client, test_dsn: str, reset_incident_state: None
) -> None:
    before = incident_row(test_dsn, "INC-001")

    payload = await call(client, "incident_acknowledge", {"incident_id": "INC-001"})

    assert payload["changed"] is True
    assert payload["previous_status"] == "open"
    assert payload["item"]["id"] == "INC-001"
    assert payload["item"]["status"] == "acknowledged"

    after = incident_row(test_dsn, "INC-001")
    assert after[:3] == before[:3]
    assert after[3] == "acknowledged"
    assert after[4:] == before[4:]


async def test_acknowledge_is_idempotent(
    client: Client, test_dsn: str, reset_incident_state: None
) -> None:
    first = await call(client, "incident_acknowledge", {"incident_id": "INC-001"})
    second = await call(client, "incident_acknowledge", {"incident_id": "INC-001"})

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["previous_status"] == "acknowledged"
    assert second["item"]["status"] == "acknowledged"
    assert incident_row(test_dsn, "INC-001")[3] == "acknowledged"


async def test_acknowledge_rejects_resolved_incident(
    client: Client, test_dsn: str, reset_incident_state: None
) -> None:
    before = incident_row(test_dsn, "INC-003")

    with pytest.raises(ToolError, match="invalid_transition"):
        await client.call_tool("incident_acknowledge", {"incident_id": "INC-003"})

    assert incident_row(test_dsn, "INC-003") == before


async def test_acknowledge_missing_incident(client: Client) -> None:
    with pytest.raises(ToolError, match="not_found"):
        await client.call_tool("incident_acknowledge", {"incident_id": "INC-999"})


@pytest.mark.parametrize("bad_id", ["INC-1", "inc-001", "INC-001; DROP TABLE incidents"])
async def test_acknowledge_rejects_malformed_ids(client: Client, bad_id: str) -> None:
    with pytest.raises(ToolError):
        await client.call_tool("incident_acknowledge", {"incident_id": bad_id})


# --- incident_create_summary ------------------------------------------------


async def test_summary_creates_missing_summary(
    client: Client, test_dsn: str, reset_incident_state: None
) -> None:
    before = incident_row(test_dsn, "INC-001")
    assert before[6] is None

    payload = await call(
        client,
        "incident_create_summary",
        {"incident_id": "INC-001", "summary": "  cache key included request_id  "},
    )

    assert payload["changed"] is True
    assert payload["previous_summary_present"] is False
    assert payload["item"]["summary"] == "cache key included request_id"
    assert incident_row(test_dsn, "INC-001")[6] == "cache key included request_id"


async def test_summary_overwrites_existing_summary(
    client: Client, test_dsn: str, reset_incident_state: None
) -> None:
    before = incident_row(test_dsn, "INC-004")
    assert before[6] == "Failover completed."

    payload = await call(
        client,
        "incident_create_summary",
        {"incident_id": "INC-004", "summary": "Root cause was the cache refactor."},
    )

    assert payload["changed"] is True
    assert payload["previous_summary_present"] is True
    assert payload["item"]["summary"] == "Root cause was the cache refactor."
    assert incident_row(test_dsn, "INC-004")[6] == "Root cause was the cache refactor."


async def test_summary_is_allowed_for_resolved_incident(
    client: Client, test_dsn: str, reset_incident_state: None
) -> None:
    payload = await call(
        client,
        "incident_create_summary",
        {"incident_id": "INC-003", "summary": "Keys rotated; incident closed."},
    )

    assert payload["changed"] is True
    assert payload["item"]["status"] == "resolved"
    assert payload["item"]["summary"] == "Keys rotated; incident closed."
    assert incident_row(test_dsn, "INC-003")[6] == "Keys rotated; incident closed."


async def test_summary_accepts_exactly_max_length(
    client: Client, reset_incident_state: None
) -> None:
    summary = "x" * 5_000

    payload = await call(
        client,
        "incident_create_summary",
        {"incident_id": "INC-001", "summary": summary},
    )

    assert payload["changed"] is True
    assert len(payload["item"]["summary"]) == 5_000


async def test_summary_is_idempotent_when_identical(
    client: Client, test_dsn: str, reset_incident_state: None
) -> None:
    payload = await call(
        client,
        "incident_create_summary",
        {"incident_id": "INC-004", "summary": "Failover completed."},
    )

    assert payload["changed"] is False
    assert payload["previous_summary_present"] is True
    assert payload["item"]["summary"] == "Failover completed."


@pytest.mark.parametrize("summary", ["", "   ", "x" * 5001])
async def test_summary_rejects_empty_or_oversized_input(
    client: Client, test_dsn: str, reset_incident_state: None, summary: str
) -> None:
    before = incident_row(test_dsn, "INC-001")

    with pytest.raises(ToolError):
        await client.call_tool(
            "incident_create_summary", {"incident_id": "INC-001", "summary": summary}
        )

    assert incident_row(test_dsn, "INC-001") == before


async def test_summary_missing_incident(client: Client) -> None:
    with pytest.raises(ToolError, match="not_found"):
        await client.call_tool(
            "incident_create_summary", {"incident_id": "INC-999", "summary": "x"}
        )


# --- concurrency ------------------------------------------------------------


async def test_concurrent_acknowledge_serializes(pool, reset_incident_state: None) -> None:
    results = await asyncio.gather(
        repositories.acknowledge_incident(pool, incident_id="INC-001"),
        repositories.acknowledge_incident(pool, incident_id="INC-001"),
    )

    assert sorted(result.changed for result in results) == [False, True]
    assert [result.previous_status.value for result in results].count("open") == 1
    assert [result.previous_status.value for result in results].count("acknowledged") == 1


async def test_concurrent_summary_last_writer_wins(
    pool, test_dsn: str, reset_incident_state: None
) -> None:
    results = await asyncio.gather(
        repositories.create_incident_summary(pool, incident_id="INC-001", summary="first"),
        repositories.create_incident_summary(pool, incident_id="INC-001", summary="second"),
    )

    assert all(result.changed is True for result in results)
    assert sorted(result.previous_summary_present for result in results) == [False, True]
    assert incident_row(test_dsn, "INC-001")[6] in {"first", "second"}


# --- write failure ----------------------------------------------------------


async def test_write_reports_unavailable_database(runtime: StandRuntime, client: Client) -> None:
    await runtime.stop()

    with pytest.raises(ToolError, match="data_source_unavailable"):
        await client.call_tool("incident_acknowledge", {"incident_id": "INC-001"})

    with pytest.raises(ToolError, match="data_source_unavailable"):
        await client.call_tool(
            "incident_create_summary", {"incident_id": "INC-001", "summary": "x"}
        )
