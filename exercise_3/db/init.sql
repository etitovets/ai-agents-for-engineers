-- Database initialization for the news pipeline.
-- Run automatically by the postgres container on first startup.

CREATE TABLE IF NOT EXISTS news (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    link        TEXT UNIQUE NOT NULL,
    published_at TIMESTAMPTZ,
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entities (
    id          BIGSERIAL PRIMARY KEY,
    news_id     BIGINT NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    label       TEXT NOT NULL,
    count       INTEGER NOT NULL CHECK (count > 0),
    UNIQUE (news_id, text, label)
);

CREATE INDEX IF NOT EXISTS idx_entities_news_id ON entities(news_id);
CREATE INDEX IF NOT EXISTS idx_news_link ON news(link);
CREATE INDEX IF NOT EXISTS idx_news_created_at ON news(created_at DESC);