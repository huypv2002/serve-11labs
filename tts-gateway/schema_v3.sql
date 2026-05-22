-- Add detail columns to usage_logs
ALTER TABLE usage_logs ADD COLUMN voice_id TEXT;
ALTER TABLE usage_logs ADD COLUMN text_preview TEXT;
ALTER TABLE usage_logs ADD COLUMN ip TEXT;
ALTER TABLE usage_logs ADD COLUMN error_msg TEXT;
ALTER TABLE usage_logs ADD COLUMN key_name TEXT;
