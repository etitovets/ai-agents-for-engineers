# Cline Rules: Russian RSS News NER Pipeline

## Mission and completion standard

Build a complete, runnable, event-driven news-processing system. Deliver working application code, Docker configuration, database schema, automated tests, and a README. Do not stop at scaffolding, pseudocode, or a design document.

The pipeline must be:

    RSS feeds -> Monitor (poller + HTTP API) -> RabbitMQ -> FastStream Worker -> PostgreSQL

The Monitor and Worker are separate containers and communicate **only through RabbitMQ**. The Worker may access PostgreSQL; the Monitor must not call the Worker or depend on its implementation. The final solution must pass the acceptance checks in this file.

## Hard technical constraints

- Use **Python 3.12** for all Python services.
- Use **uv** for dependency management. Commit `pyproject.toml` and `uv.lock`; install reproducibly in Docker with `uv sync --frozen` (or an equivalent lockfile-respecting build).
- Use **Ruff** for both formatting and linting. **After every change to Python code**, run `uv run ruff format .` and `uv run ruff check .`; fix all reported violations before proceeding. Run both once more before completion.
- Implement the Worker message handler with **FastStream**, not a hand-written RabbitMQ consumer loop.
- Use **SpaCy** with a Russian NER model, preferably `ru_core_news_sm` or `ru_core_news_md`.
- Use **RabbitMQ** as the message broker and **PostgreSQL** as the main database. Run both through `docker-compose.yml`; do not require manual local installations or start services from Python code.
- Read connection strings, credentials, polling interval, feed URLs, and other runtime options from environment-backed settings. Include `.env.example`; never commit `.env` or hardcode secrets.
- Prefer small, testable modules, type hints, clear logging, and focused error handling. Do not introduce extra infrastructure without an explicit reason.
- Preserve unrelated user files and changes; inspect the repository before editing.

## Architecture and service responsibilities

### Monitor container

- Expose a small HTTP API (FastAPI is suitable):
  - `GET /health` returns HTTP 200 and exactly `{"status":"ok"}`.
  - `POST /refresh` starts an unscheduled poll of **all configured feeds**, waits for that poll to finish, and returns HTTP 200 with `{"new_items": N}`, where `N` is the number of successfully published new messages from this invocation.
- Schedule polling at a **300-second** interval even if nobody calls `/refresh`. Keep the scheduler alive for the lifetime of the service and cancel it cleanly on shutdown.
- Avoid overlapping scheduled and manual polls. Use an async lock or another explicit single-flight strategy, and define how concurrent `/refresh` requests are handled. A manual refresh must not disable or reset periodic polling.
- Poll at least these three feeds (make them configurable with these defaults):
  - `https://ria.ru/export/rss2/archive/index.xml` (RIA Novosti)
  - `https://tass.ru/rss/v2.xml` (TASS)
  - `https://www.kommersant.ru/RSS/news.xml` (Kommersant)
- Parse each entry into `source`, `title`, `link`, `published_at`, and `text`; optionally include `guid` and diagnostic metadata. Obtain `text` from a usable content/summary/description field; do not require scraping entire article websites.
- Normalize whitespace, strip unsafe HTML markup from feed descriptions, preserve Russian Unicode, and use timezone-aware UTC datetimes where possible. Define a documented policy for missing or invalid publication dates.
- Require a stable canonical link for persistence. Skip malformed entries with useful logging rather than crashing the poll. Do not strip URL parameters indiscriminately if they identify an article.
- Deduplicate **before publishing**, not merely before database insertion. Use a persistent lightweight SQLite store mounted on a named Docker volume for Monitor deduplication; key by stable normalized article link (with source/guid metadata if useful). An in-memory set alone is not sufficient, since container restarts would replay the feed.
- Mark an item as published **only after RabbitMQ confirms successful publication**. Never mark a failed publish as seen. A crash after publication but before marking can cause redelivery; the Worker must therefore also be idempotent.
- Treat the Monitor as a single active replica when using its local SQLite store; document this assumption rather than claiming multi-replica exactly-once behavior.
- Set sensible HTTP request timeouts and a recognizable User-Agent. Handle a feed timeout, HTTP error, malformed XML, or empty feed independently so the other feeds still run. Log the source and failure context without secrets.

### RabbitMQ transport

- Use FastStream's RabbitMQ integration (`RabbitBroker`) for consistent publishing and consuming, unless a concrete technical constraint requires a different publisher.
- Use a **durable named queue** (for example, `news.incoming`) and **persistent messages**. Explicitly declare the queue from the Monitor side so publication works even when the Worker has not started.
- Use JSON messages validated by a shared Pydantic model. Required fields: `source`, `title`, `link`, `published_at`, and `text`; an optional stable entry ID is acceptable.
- Keep the message contract in a small shared Python package/module, without making either service import the other's application internals.
- Use acknowledgements compatible with at-least-once delivery. Acknowledge only after successful processing and database commit. For retriable infrastructure failures, allow controlled redelivery; avoid unbounded poison-message retry loops. Explain the chosen retry/reject behavior in the README.
- State the broker choice in **one or two README lines**: RabbitMQ provides durable queues, delivery acknowledgements, and straightforward FastStream integration.

### FastStream Worker

- Define the consumer with a declarative FastStream subscriber, such as `@broker.subscriber(...)`, receiving the typed shared message schema.
- Load the Russian SpaCy model **once at Worker startup**, never inside the per-message handler. Pin mutually compatible SpaCy/model versions and install/download the model **during the Docker image build**, not on each container start.
- Run NER on **both** title and article text. Preserve extracted labels from the model, including `PER`, `ORG`, and `LOC` when present.
- Aggregate occurrences by `(entity_text, label)` within a single article and save the occurrence count. Normalize whitespace consistently, but do not silently discard valid Russian names or labels.
- Keep CPU-heavy SpaCy inference from blocking an async event loop; use an appropriate synchronous execution path or a bounded thread execution strategy. Limit pathological input size and document any truncation policy.
- Process each message inside a robust per-message error boundary. Log structured context, and distinguish invalid message payloads from transient broker/database failures and NER failures. Do not terminate the whole Worker because one article fails.
- Allow independent Worker scaling; do not rely on process-global deduplication for correctness.

### PostgreSQL storage

- Provision the PostgreSQL service and named data volume through Docker Compose. Create the schema automatically at startup using an initialization SQL script or migrations; document which method is used.
- At minimum, create:
  - `news`: `id` primary key, `source`, `title`, `link` (**UNIQUE NOT NULL**), `published_at`, `text`, `created_at` with a database-side default.
  - `entities`: `id` primary key, `news_id` foreign key to `news.id`, `text`, `label`, `count` (positive integer).
- Add a uniqueness constraint on `entities(news_id, text, label)` and an index supporting entity lookup by `news_id`. Choose appropriate PostgreSQL types, e.g. `TIMESTAMPTZ` for publication and creation timestamps.
- Insert a news item and its entities in **one transaction**. For duplicate links, use a database-level `ON CONFLICT` strategy; never create duplicate news rows or increment entity counts on replay.
- Ensure a crash before commit does not leave a news row without its intended entities, and a redelivery after commit remains a no-op (or a safe, explicitly documented idempotent update).
- Use parameterized SQL or a database library/ORM; never interpolate feed data into SQL strings.
- Let the Worker own news/entity writes. Keep all connections and pool configuration in environment-backed settings.

## Suggested repository layout

Adapt existing structure rather than overwriting it. For a new repository, a reasonable layout is:

    pyproject.toml
    uv.lock
    .env.example
    .gitignore
    docker-compose.yml
    Dockerfile.monitor
    Dockerfile.worker
    README.md
    src/news_pipeline/config.py
    src/news_pipeline/schemas.py
    src/news_pipeline/monitor/{app.py,poller.py,feeds.py,dedup.py,publisher.py}
    src/news_pipeline/worker/{app.py,ner.py,repository.py}
    db/init.sql
    tests/{test_feeds.py,test_monitor.py,test_worker.py,test_repository.py}

Do not add files solely to match this suggested tree if a simpler, equally clear design works.

## Implementation workflow: Plan / Act and checkpoints

1. **Inspect first.** Read the existing repository, current state, and applicable rules. Identify reusable code and existing uncommitted changes before modifying files.
2. **Plan before substantial edits.** In Plan mode, or an explicit planning phase when Plan mode is unavailable, produce a concise implementation plan with service boundaries, the JSON contract, deduplication semantics, failure behavior, file plan, test strategy, and acceptance commands. Resolve architecture-level questions before coding.
3. **Use model budget deliberately.** If multiple models or architect/editor roles are actually available, reserve the stronger model for architecture, concurrency/idempotency design, and tricky debugging; use the cheaper editor model for small, bounded file edits and routine tests. Do not pretend to switch models when the tool/session does not allow it.
4. **Act in small vertical slices.** Recommended order: project setup and settings -> Compose and schema -> typed message and RabbitMQ -> Monitor/API/dedup -> Worker/NER/persistence -> tests -> README -> end-to-end verification. Keep the system runnable as much as possible.
5. **Checkpoint before risky steps.** Use Cline checkpoints when available and inspect `git status`, `git diff`, and `git diff --check` after significant changes. Do not run destructive Git operations (e.g., `git reset --hard`, `git clean -fd`) without explicit authorization. Preserve pre-existing user work.
6. **Validate continuously.** After each Python code edit, run both Ruff commands as required. Run focused tests for the edited behavior and the full test suite before finalization. If a command fails, inspect the error, fix the root cause, and rerun the failing checks.
7. **Report evidence, not intentions.** Distinguish commands actually executed and observed results from suggested commands and unverified assumptions. Never claim Docker, networking, live RSS feeds, or the end-to-end pipeline were verified unless they actually ran successfully.

## Testing requirements

- Write offline unit tests using representative RSS XML fixtures, including Russian text, missing fields, malformed entries, duplicate links, and feed failures.
- Test `GET /health` status/body and `POST /refresh` response/count. Test that a second unchanged poll publishes zero new items and that simultaneous refreshes cannot race.
- Test that failed publication does not poison the persistent seen-state; a later retry must republish the article.
- Test message validation, entity aggregation/counts, and `PER`/`ORG`/`LOC` handling using a small deterministic mock of the NER layer as appropriate.
- Test database-level idempotency for duplicate message delivery and transactional rollback for failures; use an integration test with PostgreSQL when available.
- Use mocks for external RSS and broker interactions in unit tests. Do not make routine unit tests depend on live agency feeds or network access.
- Add a lightweight Docker Compose smoke-test procedure for a real Monitor -> RabbitMQ -> Worker -> PostgreSQL round-trip.

## Docker Compose and operational requirements

- `docker-compose.yml` must define at least `rabbitmq`, `postgres`, `monitor`, and `worker` services, with restart policies as appropriate, persistent volumes, and meaningful health checks for dependencies.
- Map Monitor port **8000** to host `127.0.0.1:8000` or `8000:8000` so the required curl commands work. Use Compose service names for inter-container hostnames, never `localhost` for a broker or database in another container.
- Use dependency health conditions and application-level connection retries to tolerate startup races. Do not assume `depends_on` alone proves the application is ready.
- Keep runtime credentials and URLs in `.env`/settings and provide non-secret example values in `.env.example`. Ensure `.env` is ignored by Git.
- Avoid downloading a large language model at runtime. Build the Worker image reproducibly, and cache only ordinary build artifacts.
- Ensure the application works with `docker-compose up --build` as requested; document `docker compose up --build` as the modern CLI equivalent. Do not make an additional manually started service necessary.

## README requirements

The README must contain:

1. A short architecture diagram showing the four components and the event flow.
2. Responsibilities of each service, message schema, and deduplication/idempotency guarantees and limitations (at-least-once delivery, not exactly-once).
3. A one- or two-line justification for RabbitMQ.
4. Python/uv/Ruff/SpaCy model prerequisites and a Docker-only quickstart (`cp .env.example .env`, then Compose up).
5. Environment variables, feed URLs, persistence volumes, retry behavior, and database initialization strategy.
6. Exact API examples, expected response shapes, and the SQL verification command below.
7. Commands and results for formatting, linting, unit tests, and any real Compose/integration checks actually executed.
8. **A methodology section describing actual architect/editor and/or Plan/Act usage**: what architecture decisions used high-cost reasoning, what bounded coding work used a cheaper model (if applicable), where checkpoints and git diffs helped recovery, and where two-phase work reduced token cost versus adding latency. If model switches, checkpoints, token costs, or timing were not observed, say so; do not invent measurements or anecdotes.

## Acceptance commands and definition of done

The resulting repository must support these checks:

```bash
docker-compose up --build

curl http://127.0.0.1:8000/health
# {"status":"ok"}

curl -X POST http://127.0.0.1:8000/refresh
# {"new_items": N}

docker-compose exec postgres psql -U postgres -d news -c \
  "SELECT n.source, n.title, e.label, e.text FROM news n JOIN entities e ON e.news_id = n.id ORDER BY n.created_at DESC LIMIT 20;"
```

The PostgreSQL user/database defaults in `.env.example` must match the SQL example (`postgres` / `news`) or the README must explicitly show the corresponding configured command.

Also run:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Before declaring completion, verify and report:

- All four containers start with Compose.
- The Monitor periodically polls at 5-minute intervals without manual calls.
- `/health` and `/refresh` return the exact required JSON shapes.
- Refresh publishes items from at least three configured feeds when their feeds are reachable and have valid entries; a repeated unchanged refresh publishes no duplicates.
- The FastStream Worker consumes and performs Russian SpaCy NER once per accepted message, with the model loaded once per process.
- PostgreSQL contains linked `news` and `entities` rows, and replay of a message cannot create duplicates.
- Environment/configuration is externalized, tests pass, Ruff reports no issues, and README explains setup and the actual Plan/Act/checkpoint observations.

If a check cannot be performed (e.g., Docker daemon unavailable or a live feed unreachable), explicitly identify the unverified part, show the error or limitation, and provide the exact command to retry. Never replace missing proof with a claim of success.
