-- Supabase advisor: function_search_path_mutable on the three functions
-- created in 0001 without a SET search_path. Pin it in place.
--
-- Empty search_path is enough. pg_catalog is still searched, and these
-- bodies only call built-ins (now, coalesce, trim, lower, replace). No
-- table or extension names. ALTER FUNCTION keeps volatility (is_eligible
-- and norm_segments stay immutable) and does not touch grants or the
-- documents_touch_updated_at trigger.
--
-- Idempotent. Safe to run once on a database that already applied 0001-0003.
-- pgvector stays in public; extension_in_public is a separate follow-up.

alter function brain.touch_updated_at() set search_path = '';

alter function brain.is_eligible(text, boolean, timestamptz, timestamptz)
  set search_path = '';

alter function brain.norm_segments(text) set search_path = '';
