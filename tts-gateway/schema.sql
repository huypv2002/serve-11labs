-- API Keys table
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    email TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    active INTEGER DEFAULT 1,
    usage_count INTEGER DEFAULT 0,
    last_used TEXT,
    rate_limit INTEGER DEFAULT 10,  -- requests per minute
    expires_at TEXT
);

-- Usage logs
CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    text_length INTEGER,
    response_time_ms INTEGER,
    status INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys(key);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(active);
CREATE INDEX IF NOT EXISTS idx_usage_logs_key ON usage_logs(api_key);
CREATE INDEX IF NOT EXISTS idx_usage_logs_created ON usage_logs(created_at);
