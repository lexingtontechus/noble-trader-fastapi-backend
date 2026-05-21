-- Create the credentials table for storing Alpaca API keys per user/org.
--
-- Supports two credential types: "paper" (paper trading) and "live" (real money).
-- Keys are stored as plain text in this migration — in production, they should
-- be encrypted at the application layer (AES-256-GCM) before insertion.
--
-- RLS policies ensure users can only read their own credentials, and
-- org members can read org-level credentials.

CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    user_id TEXT NOT NULL,
    org_id TEXT,
    api_key TEXT NOT NULL,
    secret_key TEXT NOT NULL,
    credential_type TEXT NOT NULL DEFAULT 'paper' CHECK (credential_type IN ('paper', 'live')),
    is_valid BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ,
    label TEXT
);

-- Index for user-level lookups (used by resolve_alpaca_credentials)
CREATE INDEX IF NOT EXISTS idx_credentials_user_valid
ON credentials (user_id, is_valid, credential_type)
WHERE is_valid = true;

-- Enable RLS
ALTER TABLE credentials ENABLE ROW LEVEL SECURITY;

-- Users can read their own credentials
CREATE POLICY "Users can read own credentials"
ON credentials
FOR SELECT
USING (
    auth.jwt() ->> 'sub' = user_id
);

-- Users can insert their own credentials
CREATE POLICY "Users can insert own credentials"
ON credentials
FOR INSERT
WITH CHECK (
    auth.jwt() ->> 'sub' = user_id
);

-- Users can update their own credentials
CREATE POLICY "Users can update own credentials"
ON credentials
FOR UPDATE
USING (
    auth.jwt() ->> 'sub' = user_id
);

-- Users can delete their own credentials
CREATE POLICY "Users can delete own credentials"
ON credentials
FOR DELETE
USING (
    auth.jwt() ->> 'sub' = user_id
);

-- Comment
COMMENT ON TABLE credentials IS
'Alpaca API credentials stored per-user or per-org. Paper keys are resolved first, then live keys.';
