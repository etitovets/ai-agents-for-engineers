# Russian RSS News NER Pipeline

An event-driven news-processing system that polls Russian RSS feeds, extracts named entities with SpaCy, and stores results in PostgreSQL.

```
RSS feeds --> Monitor (FastAPI poller + HTTP API) --> RabbitMQ (news.incoming)
          --> FastStream Worker (SpaCy NER) --> PostgreSQL
```

## Architecture

Four components run as separate Docker containers. The Monitor and Worker communicate only through RabbitMQ; they share only the message contract (`news_pipeline/schemas.py`, `news_pipeline/transport.py`) and settings (`news_pipeline/config.py`).

| Component | Responsibility | Technology |
|-----------|---------------|------------|
| **RabbitMQ** | Durable message broker, at-least-once delivery | `rabbitmq:3-management` |
| **PostgreSQL** | Main database, schema auto-initialized via `db/init.sql` | `postgres:16` |
| **Monitor** | Polls RSS feeds at startup and then every 300s, deduplicates, publishes to RabbitMQ, exposes HTTP API | FastAPI + uvicorn + FastStream `RabbitBroker` |
| **Worker** | Consumes from RabbitMQ, runs Russian SpaCy NER, persists to PostgreSQL | FastStream + spaCy + asyncpg |

### Message contract

`NewsItem` (Pydantic) is published as JSON (`application/json`) to the durable `news.incoming` queue with `persist=True` (delivery mode 2):

- `source` (str) - feed host, e.g. `ria_ru`, `tass_ru`, `kommersant_ru`
- `title` (str) - article title (HTML stripped, whitespace normalized)
- `link` (HttpUrl) - canonical article link (query parameters kept)
- `published_at` (datetime) - timezone-aware UTC; if the feed has no parseable date, the poll time (UTC) is used and a warning is logged
- `text` (str) - from feed content/summary/description, HTML stripped; falls back to the title
- `guid` (str, optional) - feed entry ID

The AMQP `message_id` is `sha256(link)`, so every redelivery of one article has the same ID (FastStream's bounded retry counter is keyed by it).

### Deduplication and idempotency

**Monitor:** a persistent SQLite store (`/data/seen.sqlite` on the `monitor_dedup` volume) tracks published `(source, link)` pairs and is checked **before publishing**. An item is marked seen **only after RabbitMQ confirms the publish** (publisher confirms). A failed publish is never marked seen, so the next poll republishes it. A crash between publish and marking can cause a duplicate message, which the Worker handles idempotently.

**Worker:** `news.link` is `UNIQUE NOT NULL`. A news row and its entities are inserted in one transaction; `INSERT ... ON CONFLICT (link) DO NOTHING RETURNING id` returns nothing for a replay, and then no entities are touched. A crash before commit rolls back the news row together with its entities.

**Guarantee:** at-least-once delivery with idempotent storage, not exactly-once. The Monitor is assumed to be a single active replica (local SQLite dedup). Workers can be scaled independently; correctness relies on the database constraints, not on process state.

### Why RabbitMQ

RabbitMQ provides durable queues, delivery acknowledgements, and straightforward FastStream integration.

## Prerequisites

- Docker with Compose (the only requirement for the quickstart).
- For local development: Python 3.12 (pinned in `.python-version`), [uv](https://docs.astral.sh/uv/), Ruff (dev group). The SpaCy model `ru_core_news_sm` 3.7.0 is a locked dependency of the `worker` group (direct wheel URL in `pyproject.toml`), compatible with `spacy` 3.7.5. Docker installs it at image build time.

```bash
uv sync --all-groups
```

## Quickstart (Docker-only)

```bash
cp .env.example .env
docker-compose up --build      # or the modern CLI: docker compose up --build
```

Then:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}

curl -X POST http://127.0.0.1:8000/refresh
# {"new_items": N}

docker-compose exec postgres psql -U postgres -d news -c \
  "SELECT n.source, n.title, e.label, e.text FROM news n JOIN entities e ON e.news_id = n.id ORDER BY n.created_at DESC LIMIT 20;"
```

`/refresh` polls all configured feeds and returns the number of messages confirmed by RabbitMQ in that call. Polls are single-flight (one `asyncio.Lock`): a `/refresh` arriving during another poll waits for it and then runs its own poll, which typically returns `0`. Manual refreshes do not reset the 300-second schedule.

## Environment variables

See `.env.example`. All settings have defaults in `news_pipeline/config.py`.

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_SECONDS` | `300` | Monitor polling interval |
| `FEED_URLS` | RIA, TASS, Kommersant | Comma-separated RSS feed URLs |
| `HTTP_REQUEST_TIMEOUT_SECONDS` | `20` | Per-request feed timeout |
| `HTTP_USER_AGENT` | `NewsPipelineMonitor/1.0 (...)` | User-Agent sent to feeds |
| `DEDUP_DB_PATH` | `/data/seen.sqlite` | SQLite dedup database path |
| `RABBITMQ_USER` / `RABBITMQ_PASS` / `RABBITMQ_HOST` / `RABBITMQ_PORT` | `guest` / `guest` / `rabbitmq` / `5672` | Broker connection |
| `RABBITMQ_QUEUE` | `news.incoming` | Durable queue name |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_HOST` / `POSTGRES_PORT` | `postgres` / `postgres` / `news` / `postgres` / `5432` | Database connection |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | `1` / `5` | asyncpg pool size |
| `NER_MODEL` | `ru_core_news_sm` | SpaCy Russian model |
| `NER_MAX_CHARS` | `20000` | `title + "\n" + text` is truncated to this many characters before NER |
| `WORKER_MAX_RETRIES` | `3` | Requeues per message for transient failures |
| `STARTUP_RETRY_ATTEMPTS` / `STARTUP_RETRY_DELAY_SECONDS` | `10` / `3` | Connection retries at startup |
| `LOG_LEVEL` | `INFO` | Log level |

Feeds: `https://ria.ru/export/rss2/archive/index.xml`, `https://tass.ru/rss/v2.xml`, `https://www.kommersant.ru/RSS/news.xml`.

## Volumes

| Volume | Purpose |
|--------|---------|
| `postgres_data` | PostgreSQL data directory |
| `rabbitmq_data` | RabbitMQ durable queue and persistent messages |
| `monitor_dedup` | Monitor SQLite dedup database (`/data`) |

## Retry and acknowledgement behavior

- **Feeds:** each feed is fetched independently with a timeout and User-Agent. A timeout, HTTP error, network error, malformed XML or empty feed is logged with its source and does not affect other feeds. Malformed entries (no link or title) are skipped.
- **Monitor to RabbitMQ:** each publish waits for the broker confirm. A failed publish is logged and the item stays unseen, so it is retried on the next poll.
- **Startup:** the Monitor retries the RabbitMQ connection and the Worker retries the PostgreSQL connection (`STARTUP_RETRY_*`). Compose also waits for healthy dependencies and restarts services (`restart: unless-stopped`).
- **Worker acknowledgements** (FastStream subscriber with `retry=WORKER_MAX_RETRIES`):
  - success, including an idempotent duplicate: **ack** after the DB commit;
  - invalid payload (pydantic validation error): **reject** immediately (no requeue);
  - NER failure: **reject** (deterministic, so retrying cannot help; nothing is written);
  - database or other unexpected error: **nack with requeue**, up to `WORKER_MAX_RETRIES` times per Worker process, then **reject**. The counter is in memory, so a Worker restart resets it. No dead-letter queue is configured, so rejected messages are dropped after being logged.
- SpaCy inference runs in `asyncio.to_thread`, so it does not block the event loop. The model is loaded once in the FastStream `on_startup` hook.

## Database initialization

The PostgreSQL container runs `db/init.sql` (mounted into `/docker-entrypoint-initdb.d/`) on first startup of an empty data volume:

- `news(id BIGSERIAL PK, source, title, link TEXT UNIQUE NOT NULL, published_at TIMESTAMPTZ, text, created_at TIMESTAMPTZ DEFAULT now())`
- `entities(id BIGSERIAL PK, news_id FK -> news.id ON DELETE CASCADE, text, label, count INTEGER CHECK > 0, UNIQUE(news_id, text, label))`
- Indexes on `entities(news_id)` and `news(created_at DESC)`

Schema changes require recreating the volume (`docker compose down -v`) or applying SQL manually; no migration tool is used.

## Development commands

```bash
uv run ruff format .
uv run ruff check .
uv run ruff format --check .
uv run pytest
# PostgreSQL integration tests (otherwise skipped), against the Compose database:
TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/news uv run pytest tests/test_repository.py
```

Tests are offline: RSS is served by `httpx.MockTransport`, RabbitMQ is replaced by FastStream's `TestRabbitBroker`, NER uses a deterministic fake (plus one test with the real `ru_core_news_sm` model), and the SQLite dedup store is real (temp file).

## Verification results (2026-11-19, observed)

Local checks (Python 3.12.16):

```
uv run ruff format --check .   -> 21 files already formatted
uv run ruff check .            -> All checks passed!
uv run pytest                  -> 33 passed, 2 skipped (DB tests without TEST_DATABASE_URL)
TEST_DATABASE_URL=... pytest   -> 35 passed
```

Docker Compose round-trip (`docker compose up --build -d`, live feeds reachable):

- All four containers were up; postgres and rabbitmq were healthy.
- On startup the Monitor published 713 items (RIA 99, TASS 99, Kommersant 515; Kommersant's 301 redirect to `/rss/news.xml` was followed).
- The Worker logged `SpaCy model ru_core_news_sm loaded` once and then stored 713 news rows with PER/ORG/LOC entities.
- `curl /health` -> `{"status":"ok"}` (HTTP 200); `curl -X POST /refresh` -> `{"new_items":6}` (new articles since startup), then immediately again -> `{"new_items":0}`.
- SQL query above returned 20 rows of linked news and entities; `SELECT count(*) - count(DISTINCT link) FROM news` -> `0`.
- Replay: publishing an already-stored article twice directly to RabbitMQ left news/entity/occurrence counts unchanged (`719|3038|3731` before and after); the Worker logged `Duplicate link ... ignored`.
- Invalid message `{"title": "broken"}` was rejected and the queue drained to 0 ready / 0 unacked.
- After `docker compose restart monitor`, the startup poll published only 1 new article (dedup state survived the restart).
- Scheduled poll: with no manual calls, the Monitor polled at 07:24:48 UTC, 300 s after the previous poll at 07:19:47, and published 17 new items (RIA 2, TASS 14, Kommersant 1). Queue afterwards: 0 ready / 0 unacked.

## Troubleshooting history (fixed issues)

The first `docker compose up` failed. These issues were found from the logs and code review and are now fixed:

1. Both app containers exited with `ModuleNotFoundError: No module named 'pydantic._internal._annotated_handlers'`: `faststream` 0.4.7 is incompatible with `pydantic` 2.14. Upgraded to `faststream[rabbit,cli]` 0.5.48 (the `cli` extra is required for `faststream run`).
2. The Worker used `@broker.lifespan`, which does not exist; replaced with `FastStream.on_startup` / `on_shutdown`.
3. `DATABASE_URL` used `postgresql+asyncpg://`, which asyncpg rejects; now `postgresql://`.
4. The Monitor called `broker.declare_queue(name, durable=True)` (wrong signature, so every publish failed) and published `json.dumps(...)` strings without `persist=True`. It now declares `RabbitQueue(..., durable=True)` and publishes the `NewsItem` model as persistent JSON.
5. Feeds were downloaded by `feedparser.parse(url)` without timeout or User-Agent and blocking the event loop; replaced with `httpx.AsyncClient`.
6. `/refresh` opened a new broker connection and SQLite store per call; it now reuses the ones created in the lifespan.
7. Invalid-payload rejection was first implemented as a custom FastStream decoder. The compose check showed the invalid message stuck as unacked: decoders run before FastStream's acknowledgement scope. It was moved to a subscriber middleware, and a test now asserts `reject()` is called.
8. The local `.venv` was Python 3.15 and tests used stub modules for `faststream`, `spacy` and `asyncpg`, which hid issues 1-4. Local dev is pinned to Python 3.12 and the stubs were removed.

## Methodology

- **Plan/Act:** this repair session started in Plan mode. Read-only inspection (existing container logs, `uv.lock` versions, source) produced the root cause and a list of latent bugs; one scope question was asked (runtime-only fix vs. also moving tests to real libraries), and the user chose the full fix. The plan was then implemented in Agent mode in slices: dependencies, Worker, Monitor, tests, Compose verification.
- **High-cost reasoning** went into acknowledgement semantics: reading FastStream 0.5 source showed that `retry=N` counts by `message_id` (hence the deterministic `sha256(link)` ID) and that decoders run outside the ack scope (found only through the real Compose run, then fixed).
- **Models:** a single model was used for the whole session; no architect/editor model switch happened.
- **Checkpoints:** the files were untracked in git, so `git diff` was not useful for recovery. The original Dockerfiles remain as `Dockerfile.*.bak`. No destructive git commands were run. Token costs and timings were not measured.
- **Trade-off observed:** the planning phase found most bugs up front through reading code, but the decoder/ack-scope issue appeared only during end-to-end verification. Real-broker checks are still needed in addition to unit tests.
