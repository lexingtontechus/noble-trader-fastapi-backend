-- Add org_id column to credentials table for organization-level multi-tenancy.
--
-- When a user belongs to a Clerk Organization and selects it, the JWT carries
-- an org_id claim. Credentials with a matching org_id take priority over
-- user-level credentials during resolution.
--
-- Migration: 00000000000016_org_credentials

-- Add org_id column (nullable — existing user-level rows remain valid)
ALTER TABLE credentials
ADD COLUMN IF NOT EXISTS org_id TEXT;

-- Create index for fast org-level credential lookups
CREATE INDEX IF NOT EXISTS idx_credentials_org_id
ON credentials (org_id)
WHERE org_id IS NOT NULL;

-- Create composite index for the exact query pattern used by resolve_alpaca_credentials
CREATE INDEX IF NOT EXISTS idx_credentials_org_valid
ON credentials (org_id, is_valid, credential_type)
WHERE org_id IS NOT NULL AND is_valid = true;

-- Add RLS policy: org members can read org-level credentials
CREATE POLICY "Org members can read org credentials"
ON credentials
FOR SELECT
USING (
  org_id IS NOT NULL
  AND auth.jwt() ->> 'org_id' = org_id
);

-- Comment
COMMENT ON COLUMN credentials.org_id IS
'Clerk Organization ID. When set, these credentials are resolved for all members of the org. Takes priority over user-level credentials.';
