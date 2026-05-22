-- Add balance and pricing to api_keys
ALTER TABLE api_keys ADD COLUMN balance INTEGER DEFAULT 0;  -- credits in VND
ALTER TABLE api_keys ADD COLUMN total_spent INTEGER DEFAULT 0;  -- total spent

-- Pricing: 1 credit = 1 VND, cost = chars * rate
-- Default rate: 1 VND/char → 800 char request = 800 VND (~$0.03)

-- Add cost to usage_logs
ALTER TABLE usage_logs ADD COLUMN cost INTEGER DEFAULT 0;
ALTER TABLE usage_logs ADD COLUMN chars INTEGER DEFAULT 0;

-- Transactions table for top-ups
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key TEXT NOT NULL,
    amount INTEGER NOT NULL,  -- positive = top-up, negative = usage
    type TEXT NOT NULL,  -- 'topup', 'usage', 'refund'
    note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_transactions_key ON transactions(api_key);
