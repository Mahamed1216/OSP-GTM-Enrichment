-- OSP GTM Enrichment - Supabase schema
--
-- Generated from src/models.py by scripts/gen_supabase_schema.py. Do not edit
-- by hand: re-run the generator instead, or the file will drift from the code.
--
-- Safe to run on a fresh Supabase project, and safe to re-run: every statement
-- is IF NOT EXISTS / idempotent.
--
-- No extensions are required. Every primary key is a SERIAL integer; nothing in
-- this schema uses uuid or pgcrypto.
--
-- Paste the whole file into the Supabase SQL Editor and run it. See
-- supabase/README.md for the full setup, including the DATABASE_URL to give
-- Vercel.

-- ---------------------------------------------------------------------------
-- api_runs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_runs (
	id SERIAL NOT NULL, 
	run_id VARCHAR(64) NOT NULL, 
	workspace_id INTEGER, 
	source VARCHAR(64), 
	run_mode VARCHAR(16) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	lead_count INTEGER NOT NULL, 
	processed_count INTEGER NOT NULL, 
	failed_count INTEGER NOT NULL, 
	request_payload JSON, 
	result_payload JSON, 
	error TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	completed_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_api_runs_run_id ON api_runs (run_id);
CREATE INDEX IF NOT EXISTS ix_api_runs_status ON api_runs (status);
CREATE INDEX IF NOT EXISTS ix_api_runs_workspace_id ON api_runs (workspace_id);
COMMENT ON TABLE api_runs IS 'Audit and async-tracking record for one internal-API process request.';

-- ---------------------------------------------------------------------------
-- instantly_analytics_snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instantly_analytics_snapshots (
	id SERIAL NOT NULL, 
	campaign_id VARCHAR(64) NOT NULL, 
	leads_count INTEGER NOT NULL, 
	contacted_count INTEGER NOT NULL, 
	emails_sent_count INTEGER NOT NULL, 
	open_count INTEGER NOT NULL, 
	unique_open_count INTEGER, 
	reply_count INTEGER NOT NULL, 
	bounced_count INTEGER NOT NULL, 
	click_count INTEGER NOT NULL, 
	unsubscribed_count INTEGER NOT NULL, 
	completed_count INTEGER NOT NULL, 
	positive_reply_count INTEGER, 
	opportunity_count INTEGER, 
	conversion_count INTEGER, 
	raw_positive_reply_source VARCHAR(128), 
	raw_opportunity_source VARCHAR(128), 
	raw JSON NOT NULL, 
	synced_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	workspace_id INTEGER, 
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_instantly_analytics_snapshots_campaign_id ON instantly_analytics_snapshots (campaign_id);
CREATE INDEX IF NOT EXISTS ix_instantly_analytics_snapshots_synced_at ON instantly_analytics_snapshots (synced_at);
COMMENT ON TABLE instantly_analytics_snapshots IS 'Raw and parsed result of one Instantly campaign analytics poll.';

-- ---------------------------------------------------------------------------
-- lead_source_imports
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead_source_imports (
	id SERIAL NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	source_name VARCHAR(64), 
	base_url VARCHAR(512), 
	client_slug VARCHAR(128), 
	icp_filter VARCHAR(128), 
	status_filter VARCHAR(64), 
	include_suppressed BOOLEAN NOT NULL, 
	started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITHOUT TIME ZONE, 
	status VARCHAR(32) NOT NULL, 
	requested_limit INTEGER, 
	fetched_count INTEGER NOT NULL, 
	created_count INTEGER NOT NULL, 
	updated_count INTEGER NOT NULL, 
	skipped_count INTEGER NOT NULL, 
	error_count INTEGER NOT NULL, 
	error_message TEXT, 
	raw_summary JSON, 
	auto_run BOOLEAN NOT NULL, 
	processed_count INTEGER NOT NULL, 
	scored_count INTEGER NOT NULL, 
	content_generated_count INTEGER NOT NULL, 
	enrichment_skipped_count INTEGER NOT NULL, 
	triggered_run_id VARCHAR(128), 
	triggered_run_status VARCHAR(32), 
	source_signal_count INTEGER NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_lead_source_imports_workspace_id ON lead_source_imports (workspace_id);
COMMENT ON TABLE lead_source_imports IS 'Audit record for one pull-based import from the external lead source API.';

-- ---------------------------------------------------------------------------
-- leads
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
	id SERIAL NOT NULL, 
	first_name VARCHAR(128) NOT NULL, 
	last_name VARCHAR(128) NOT NULL, 
	email VARCHAR(256) NOT NULL, 
	title VARCHAR(256), 
	company VARCHAR(256), 
	company_domain VARCHAR(256), 
	linkedin_url VARCHAR(512), 
	company_linkedin_url VARCHAR(512), 
	industry VARCHAR(256), 
	email_verification_status VARCHAR(32), 
	email_verification_provider VARCHAR(32), 
	email_verified_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	workspace_id INTEGER, 
	external_contact_id VARCHAR(256), 
	external_source VARCHAR(64), 
	external_client_slug VARCHAR(128), 
	phone VARCHAR(64), 
	lead_source_raw JSON, 
	source_tier VARCHAR(16), 
	source_tier_score FLOAT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_leads_email_workspace UNIQUE (email, workspace_id)
);
CREATE INDEX IF NOT EXISTS ix_leads_email ON leads (email);
CREATE INDEX IF NOT EXISTS ix_leads_external_contact_id ON leads (external_contact_id);
COMMENT ON TABLE leads IS 'The contacts the pipeline works on. Unique on (email, workspace_id). Required to boot.';

-- ---------------------------------------------------------------------------
-- prompt_configs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prompt_configs (
	id SERIAL NOT NULL, 
	channel VARCHAR(32) NOT NULL, 
	content TEXT NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	prompt_version VARCHAR(32), 
	prompt_fingerprint VARCHAR(64), 
	updated_by VARCHAR(64), 
	is_active BOOLEAN NOT NULL, 
	workspace_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_prompt_configs_channel_workspace UNIQUE (channel, workspace_id)
);
CREATE INDEX IF NOT EXISTS ix_prompt_configs_channel ON prompt_configs (channel);
COMMENT ON TABLE prompt_configs IS 'User-edited overrides for the content-generation system prompts, unique per (channel, workspace_id).';

-- ---------------------------------------------------------------------------
-- prompt_recommendations
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prompt_recommendations (
	id SERIAL NOT NULL, 
	bottleneck VARCHAR(32) NOT NULL, 
	channel VARCHAR(32), 
	diagnosis TEXT NOT NULL, 
	current_metric_label VARCHAR(64) NOT NULL, 
	current_metric_value FLOAT NOT NULL, 
	target_metric_value FLOAT NOT NULL, 
	recommended_change TEXT NOT NULL, 
	expected_impact TEXT NOT NULL, 
	risk_level VARCHAR(16) NOT NULL, 
	proposed_prompt TEXT, 
	proposed_addendum TEXT, 
	previous_prompt_snapshot TEXT, 
	sample_size INTEGER NOT NULL, 
	low_confidence BOOLEAN NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	loop_status VARCHAR(32), 
	confidence VARCHAR(16), 
	metric_snapshot JSON, 
	analytics_snapshot_id INTEGER, 
	approved_by VARCHAR(64), 
	approved_at TIMESTAMP WITHOUT TIME ZONE, 
	rejected_at TIMESTAMP WITHOUT TIME ZONE, 
	drafted_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	workspace_id INTEGER, 
	PRIMARY KEY (id)
);
COMMENT ON TABLE prompt_recommendations IS 'Self-improvement-loop prompt recommendation, gated on human approval.';

-- ---------------------------------------------------------------------------
-- reply_drafts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reply_drafts (
	id SERIAL NOT NULL, 
	workspace_id INTEGER, 
	lead_id INTEGER, 
	inbound_reply TEXT NOT NULL, 
	original_outbound_email TEXT, 
	lead_context TEXT, 
	classification VARCHAR(64) NOT NULL, 
	recommended_action VARCHAR(64) NOT NULL, 
	draft_body TEXT NOT NULL, 
	human_review_notes TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	PRIMARY KEY (id)
);
COMMENT ON TABLE reply_drafts IS 'Reply draft produced by the Manual Draft Tester. Never sent automatically.';

-- ---------------------------------------------------------------------------
-- reply_threads
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reply_threads (
	id SERIAL NOT NULL, 
	workspace_id INTEGER, 
	lead_id INTEGER, 
	campaign_id VARCHAR(128) NOT NULL, 
	instantly_lead_id VARCHAR(256), 
	prospect_email VARCHAR(256) NOT NULL, 
	prospect_name VARCHAR(256), 
	company_name VARCHAR(256), 
	thread_id VARCHAR(256), 
	message_id VARCHAR(256), 
	inbound_reply_text TEXT NOT NULL, 
	original_outbound_email TEXT, 
	reply_received_at TIMESTAMP WITHOUT TIME ZONE, 
	classification VARCHAR(64) NOT NULL, 
	recommended_action VARCHAR(64) NOT NULL, 
	draft_body TEXT NOT NULL, 
	human_review_notes TEXT NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	sent_at TIMESTAMP WITHOUT TIME ZONE, 
	send_error TEXT, 
	raw_payload JSON, 
	dedup_key VARCHAR(128) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_reply_threads_workspace_campaign_dedup UNIQUE (workspace_id, campaign_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS ix_reply_threads_campaign_id ON reply_threads (campaign_id);
CREATE INDEX IF NOT EXISTS ix_reply_threads_status ON reply_threads (status);
CREATE INDEX IF NOT EXISTS ix_reply_threads_workspace_id ON reply_threads (workspace_id);
COMMENT ON TABLE reply_threads IS 'One inbound reply synced from Instantly, with its auto-generated draft.';

-- ---------------------------------------------------------------------------
-- winning_examples
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS winning_examples (
	id SERIAL NOT NULL, 
	lead_context JSON NOT NULL, 
	subject VARCHAR(512) NOT NULL, 
	body TEXT NOT NULL, 
	reply_rate FLOAT NOT NULL, 
	manually_flagged BOOLEAN NOT NULL, 
	promoted_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	workspace_id INTEGER, 
	content_type VARCHAR(32), 
	PRIMARY KEY (id)
);
COMMENT ON TABLE winning_examples IS 'Legacy DB-backed winners library. Superseded by the JSON library.';

-- ---------------------------------------------------------------------------
-- workspaces
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspaces (
	id SERIAL NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	is_default BOOLEAN NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	instantly_campaign_id VARCHAR(128), 
	instantly_api_key VARCHAR(256), 
	notes TEXT, 
	icp_config JSON, 
	lead_source_config JSON, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_workspaces_slug ON workspaces (slug);
COMMENT ON TABLE workspaces IS 'A named operating context (one per client or campaign). Every other table is scoped to a workspace via workspace_id. Required to boot.';

-- ---------------------------------------------------------------------------
-- enrichments
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrichments (
	id SERIAL NOT NULL, 
	lead_id INTEGER NOT NULL, 
	linkedin_profile JSON, 
	linkedin_posts JSON, 
	company_details JSON, 
	company_posts JSON, 
	company_news JSON, 
	industry_news JSON, 
	buyer_accounts JSON, 
	source_status JSON NOT NULL, 
	enriched_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	workspace_id INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (lead_id), 
	FOREIGN KEY(lead_id) REFERENCES leads (id)
);
COMMENT ON TABLE enrichments IS 'Enrichment result for one lead (LinkedIn, company, buyer research).';

-- ---------------------------------------------------------------------------
-- generated_contents
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS generated_contents (
	id SERIAL NOT NULL, 
	lead_id INTEGER NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	subject VARCHAR(512), 
	body TEXT NOT NULL, 
	signals_cited JSON NOT NULL, 
	prompt_version VARCHAR(32) NOT NULL, 
	prompt_fingerprint VARCHAR(64), 
	model VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	delivered_at TIMESTAMP WITHOUT TIME ZONE, 
	delivery_provider VARCHAR(32), 
	delivery_id VARCHAR(256), 
	skip_reason VARCHAR(64), 
	delivery_status VARCHAR(32), 
	error_message TEXT, 
	superseded_by_id INTEGER, 
	workspace_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(lead_id) REFERENCES leads (id), 
	FOREIGN KEY(superseded_by_id) REFERENCES generated_contents (id)
);
CREATE INDEX IF NOT EXISTS ix_generated_contents_lead_id ON generated_contents (lead_id);
COMMENT ON TABLE generated_contents IS 'One row per generated artifact - email, call script or LinkedIn DM.';

-- ---------------------------------------------------------------------------
-- lead_signals
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead_signals (
	id SERIAL NOT NULL, 
	lead_id INTEGER NOT NULL, 
	workspace_id INTEGER, 
	signal_type VARCHAR(32) NOT NULL, 
	signal_found BOOLEAN NOT NULL, 
	signal_strength VARCHAR(16) NOT NULL, 
	relevant_roles JSON NOT NULL, 
	relevant_departments JSON NOT NULL, 
	recency_estimate VARCHAR(32), 
	summary TEXT, 
	why_it_matters TEXT, 
	source_urls JSON NOT NULL, 
	recommended_email_angle TEXT, 
	tier_uplift_recommendation VARCHAR(16) NOT NULL, 
	applied_uplift VARCHAR(16), 
	base_tier VARCHAR(1), 
	base_score INTEGER, 
	status VARCHAR(16) NOT NULL, 
	last_run_at TIMESTAMP WITHOUT TIME ZONE, 
	error TEXT, 
	raw_payload JSON, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_lead_signals_lead_type UNIQUE (lead_id, signal_type), 
	FOREIGN KEY(lead_id) REFERENCES leads (id)
);
CREATE INDEX IF NOT EXISTS ix_lead_signals_lead_id ON lead_signals (lead_id);
CREATE INDEX IF NOT EXISTS ix_lead_signals_signal_type ON lead_signals (signal_type);
CREATE INDEX IF NOT EXISTS ix_lead_signals_workspace_id ON lead_signals (workspace_id);
COMMENT ON TABLE lead_signals IS 'Buying-intent signal discovered for a lead (hiring, imported source signals).';

-- ---------------------------------------------------------------------------
-- scores
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scores (
	id SERIAL NOT NULL, 
	lead_id INTEGER NOT NULL, 
	score INTEGER NOT NULL, 
	tier VARCHAR(1) NOT NULL, 
	rationale TEXT NOT NULL, 
	signals_used JSON NOT NULL, 
	model VARCHAR(64) NOT NULL, 
	scored_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	workspace_id INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (lead_id), 
	FOREIGN KEY(lead_id) REFERENCES leads (id)
);
COMMENT ON TABLE scores IS 'ICP fit score and tier (A/B/C/D) for one lead.';

-- ---------------------------------------------------------------------------
-- content_ratings
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS content_ratings (
	id SERIAL NOT NULL, 
	generated_content_id INTEGER NOT NULL, 
	rating VARCHAR(8) NOT NULL, 
	feedback_text TEXT, 
	rated_by VARCHAR(64) NOT NULL, 
	rated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	workspace_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(generated_content_id) REFERENCES generated_contents (id)
);
CREATE INDEX IF NOT EXISTS ix_content_ratings_generated_content_id ON content_ratings (generated_content_id);
COMMENT ON TABLE content_ratings IS 'Human ratings of generated content, feeding the self-improvement loop.';

-- ---------------------------------------------------------------------------
-- engagements
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS engagements (
	id SERIAL NOT NULL, 
	content_id INTEGER NOT NULL, 
	sent BOOLEAN NOT NULL, 
	delivered BOOLEAN NOT NULL, 
	opened BOOLEAN NOT NULL, 
	clicked BOOLEAN NOT NULL, 
	replied BOOLEAN NOT NULL, 
	reply_sentiment VARCHAR(32), 
	bounced BOOLEAN NOT NULL, 
	raw JSON, 
	synced_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	workspace_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(content_id) REFERENCES generated_contents (id)
);
CREATE INDEX IF NOT EXISTS ix_engagements_content_id ON engagements (content_id);
COMMENT ON TABLE engagements IS 'Per-lead delivery and engagement events synced back from Instantly.';

-- ---------------------------------------------------------------------------
-- Default workspace
-- ---------------------------------------------------------------------------
-- The app expects one default workspace to exist. Normally seed_default_workspace()
-- creates it, but that only runs from init_db() - which the Vercel function does
-- not call - so seed it here to make the pure-SQL path complete.
INSERT INTO workspaces (name, slug, is_default, is_active, created_at, updated_at)
SELECT 'OSP', 'osp', TRUE, TRUE, (now() AT TIME ZONE 'utc'), (now() AT TIME ZONE 'utc')
WHERE NOT EXISTS (SELECT 1 FROM workspaces WHERE slug = 'osp');

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
-- The app connects straight to Postgres as the table owner, so it bypasses RLS
-- and needs no policies. But Supabase also exposes every public table through
-- PostgREST, where the project's anon key would otherwise be able to read and
-- write this data. Enabling RLS with no policies closes that door while leaving
-- the app's direct connection working.
--
-- Only remove this if you deliberately want the tables reachable from the
-- Supabase REST/client SDKs, and then add policies rather than disabling RLS.
ALTER TABLE api_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE instantly_analytics_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead_source_imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_recommendations ENABLE ROW LEVEL SECURITY;
ALTER TABLE reply_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE reply_threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE winning_examples ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE enrichments ENABLE ROW LEVEL SECURITY;
ALTER TABLE generated_contents ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead_signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_ratings ENABLE ROW LEVEL SECURITY;
ALTER TABLE engagements ENABLE ROW LEVEL SECURITY;
