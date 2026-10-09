-- Complete pre-deletion schema, DDL only; no production rows or credentials.
CREATE TABLE admin_credentials (
	id INTEGER NOT NULL,
	username VARCHAR(80) NOT NULL,
	password_hash VARCHAR(255) NOT NULL,
	session_epoch INTEGER NOT NULL,
	failed_attempts INTEGER NOT NULL,
	locked_until DATETIME,
	initialized_at DATETIME NOT NULL,
	password_changed_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (username)
);
CREATE TABLE admin_sessions (
	token_hash VARCHAR(64) NOT NULL,
	admin_id INTEGER NOT NULL,
	session_epoch INTEGER NOT NULL,
	csrf_hash VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	last_seen_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	revoked_at DATETIME,
	PRIMARY KEY (token_hash),
	FOREIGN KEY(admin_id) REFERENCES admin_credentials (id) ON DELETE CASCADE
);
CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL,
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
CREATE TABLE approval_requests (
	id VARCHAR(36) NOT NULL,
	run_id VARCHAR(36) NOT NULL,
	plan_hash VARCHAR(64) NOT NULL,
	snapshot_hash VARCHAR(64) NOT NULL,
	plan_json JSON NOT NULL,
	status VARCHAR(20) NOT NULL,
	expires_at DATETIME NOT NULL,
	approved_at DATETIME,
	approved_by VARCHAR(80),
	cancelled_at DATETIME,
	invalidated_at DATETIME,
	invalid_reason TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_approval_status CHECK (status IN ('PENDING','APPROVED','CANCELLED','INVALIDATED','EXPIRED')),
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE
);
CREATE TABLE evidence (
	id VARCHAR(36) NOT NULL,
	run_id VARCHAR(36) NOT NULL,
	site_run_id VARCHAR(36),
	kind VARCHAR(40) NOT NULL,
	file_path VARCHAR(500) NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	size_bytes INTEGER,
	metadata JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE,
	FOREIGN KEY(site_run_id) REFERENCES site_runs (id) ON DELETE CASCADE
);
CREATE TABLE feedback_reviews (
	id VARCHAR(36) NOT NULL,
	run_id VARCHAR(36),
	store_id INTEGER NOT NULL,
	marketplace_code VARCHAR(2) NOT NULL,
	order_id VARCHAR(40) NOT NULL,
	rating INTEGER NOT NULL,
	order_date VARCHAR(20),
	comment TEXT,
	category VARCHAR(40),
	reason_code VARCHAR(8),
	decision_source VARCHAR(16),
	decision_note TEXT,
	state VARCHAR(24) NOT NULL,
	submitted_at DATETIME,
	details JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL, amazon_removed BOOLEAN DEFAULT 0 NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_feedback_review_rating CHECK (rating BETWEEN 1 AND 5),
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE SET NULL,
	FOREIGN KEY(store_id) REFERENCES stores (id) ON DELETE CASCADE,
	CONSTRAINT uq_feedback_review_order UNIQUE (store_id, marketplace_code, order_id)
);
CREATE TABLE notification_deliveries (
	id INTEGER NOT NULL,
	run_id VARCHAR(36) NOT NULL,
	site_run_id VARCHAR(36),
	dedupe_key VARCHAR(255) NOT NULL,
	kind VARCHAR(64) NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempts INTEGER NOT NULL,
	sent_at DATETIME,
	last_error TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_notification_delivery_status CHECK (status IN ('PENDING','SENT','FAILED')),
	CONSTRAINT ck_notification_delivery_attempts CHECK (attempts >= 0),
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE,
	FOREIGN KEY(site_run_id) REFERENCES site_runs (id) ON DELETE CASCADE,
	CONSTRAINT uq_notification_deliveries_dedupe_key UNIQUE (dedupe_key)
);
CREATE TABLE "operation_guards" (
	id VARCHAR(36) NOT NULL,
	guard_key VARCHAR(255) NOT NULL,
	run_id VARCHAR(36) NOT NULL,
	site_run_id VARCHAR(36) NOT NULL,
	store_id INTEGER NOT NULL,
	workflow VARCHAR(80) NOT NULL,
	marketplace_code VARCHAR(2) NOT NULL,
	settlement_key VARCHAR(180) NOT NULL,
	state VARCHAR(32) NOT NULL,
	amount NUMERIC(18, 2) NOT NULL,
	currency VARCHAR(3) NOT NULL,
	plan_hash VARCHAR(64) NOT NULL,
	snapshot_hash VARCHAR(64) NOT NULL,
	armed_at DATETIME NOT NULL,
	submitted_at DATETIME,
	confirmed_at DATETIME,
	last_reconciled_at DATETIME,
	failure_reason TEXT,
	metadata JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL, payout_account_tail VARCHAR(8),
	PRIMARY KEY (id),
	CONSTRAINT ck_guard_state CHECK (state IN ('ARMED','SUBMITTED','CONFIRMED','UNCERTAIN','CANCELLED')),
	CONSTRAINT ck_guard_amount CHECK (amount >= 0),
	UNIQUE (guard_key),
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE RESTRICT,
	FOREIGN KEY(store_id) REFERENCES stores (id) ON DELETE RESTRICT,
	FOREIGN KEY(site_run_id) REFERENCES site_runs (id) ON DELETE RESTRICT
);
CREATE TABLE run_events (
	id INTEGER NOT NULL,
	run_id VARCHAR(36) NOT NULL,
	site_run_id VARCHAR(36),
	event_type VARCHAR(80) NOT NULL,
	from_status VARCHAR(40),
	to_status VARCHAR(40),
	message TEXT NOT NULL,
	details JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE,
	FOREIGN KEY(site_run_id) REFERENCES site_runs (id) ON DELETE CASCADE
);
CREATE TABLE "run_queue_entries" (
	id INTEGER NOT NULL,
	run_id VARCHAR(36) NOT NULL,
	action VARCHAR(24) NOT NULL,
	priority INTEGER NOT NULL,
	state VARCHAR(16) NOT NULL,
	scheduled_for_at DATETIME,
	enqueued_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
	available_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
	claim_token VARCHAR(64),
	claimed_at DATETIME,
	finished_at DATETIME,
	cross_day_notified_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	business_priority INTEGER DEFAULT '1' NOT NULL,
	target_order INTEGER DEFAULT '2147483647' NOT NULL,
	batch_scope_id INTEGER,
	PRIMARY KEY (id),
	CONSTRAINT ck_run_queue_state CHECK (state IN ('READY','CLAIMED','DONE','CANCELLED')),
	CONSTRAINT ck_run_queue_priority CHECK (priority IN (0,10,100)),
	CONSTRAINT ck_run_queue_action CHECK (action IN ('START','APPROVE','CONTINUE_AUTH','RECONCILE')),
	CONSTRAINT ck_run_queue_business_priority CHECK (business_priority >= 0),
	CONSTRAINT ck_run_queue_target_order CHECK (target_order >= 1),
	CONSTRAINT ck_run_queue_batch_scope_id CHECK (batch_scope_id IS NULL OR batch_scope_id >= 1),
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE
);
CREATE TABLE "runs" (
	id VARCHAR(36) NOT NULL,
	store_id INTEGER NOT NULL,
	schedule_id INTEGER,
	workflow VARCHAR(80) NOT NULL,
	mode VARCHAR(16) NOT NULL,
	"trigger" VARCHAR(16) NOT NULL,
	status VARCHAR(40) NOT NULL,
	requested_by VARCHAR(80) NOT NULL,
	started_at DATETIME,
	finished_at DATETIME,
	auth_deadline DATETIME,
	error TEXT,
	result_summary JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	scheduled_for_at DATETIME,
	workflow_config JSON DEFAULT '{}' NOT NULL,
	workflow_config_version INTEGER DEFAULT '1' NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_run_trigger CHECK (trigger IN ('manual','schedule','recovery')),
	CONSTRAINT ck_run_mode CHECK (mode IN ('dry_run','approval','auto')),
	CONSTRAINT ck_run_workflow_config_version CHECK (workflow_config_version >= 1),
	FOREIGN KEY(store_id) REFERENCES stores (id) ON DELETE RESTRICT,
	FOREIGN KEY(schedule_id) REFERENCES schedules (id) ON DELETE SET NULL
);
CREATE TABLE schedule_batches (
	id INTEGER NOT NULL,
	request_id VARCHAR(36) NOT NULL,
	definition_hash VARCHAR(64) NOT NULL,
	definition_json JSON NOT NULL,
	schedule_count INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_schedule_batch_count CHECK (schedule_count >= 0),
	CONSTRAINT uq_schedule_batches_request_id UNIQUE (request_id)
);
CREATE TABLE "schedules" (
	id INTEGER NOT NULL,
	store_id INTEGER NOT NULL,
	name VARCHAR(160) NOT NULL,
	workflow VARCHAR(80) NOT NULL,
	mode VARCHAR(16) NOT NULL,
	timezone VARCHAR(64) NOT NULL,
	marketplace_codes JSON NOT NULL,
	enabled BOOLEAN NOT NULL,
	misfire_grace_seconds INTEGER NOT NULL,
	last_run_at DATETIME,
	next_run_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	batch_id INTEGER,
	batch_order INTEGER,
	workflow_config JSON DEFAULT '{}' NOT NULL,
	workflow_config_version INTEGER DEFAULT '1' NOT NULL,
	interval_minutes INTEGER DEFAULT (1440) NOT NULL,
	first_run_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_schedule_batch_order CHECK (batch_order IS NULL OR batch_order >= 1),
	CONSTRAINT ck_schedule_mode CHECK (mode IN ('dry_run','approval','auto')),
	CONSTRAINT ck_misfire_grace CHECK (misfire_grace_seconds BETWEEN 0 AND 1800),
	CONSTRAINT uq_schedule_batch_order UNIQUE (batch_id, batch_order),
	CONSTRAINT fk_schedules_batch_id_schedule_batches FOREIGN KEY(batch_id) REFERENCES schedule_batches (id) ON DELETE SET NULL,
	CONSTRAINT ck_schedule_workflow_config_version CHECK (workflow_config_version >= 1),
	FOREIGN KEY(store_id) REFERENCES stores (id) ON DELETE CASCADE
);
CREATE TABLE site_runs (
	id VARCHAR(36) NOT NULL,
	run_id VARCHAR(36) NOT NULL,
	marketplace_id INTEGER NOT NULL,
	marketplace_code VARCHAR(2) NOT NULL,
	status VARCHAR(40) NOT NULL,
	currency VARCHAR(3),
	payable_amount NUMERIC(18, 2),
	delayed_amount NUMERIC(18, 2),
	settlement_key VARCHAR(180),
	plan_hash VARCHAR(64),
	snapshot_hash VARCHAR(64),
	started_at DATETIME,
	finished_at DATETIME,
	error TEXT,
	details JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_run_marketplace UNIQUE (run_id, marketplace_id),
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE,
	FOREIGN KEY(marketplace_id) REFERENCES store_marketplaces (id) ON DELETE RESTRICT
);
CREATE TABLE "store_marketplaces" (
	id INTEGER NOT NULL,
	store_id INTEGER NOT NULL,
	code VARCHAR(2) NOT NULL,
	domain VARCHAR(255) NOT NULL,
	currency VARCHAR(3) NOT NULL,
	enabled BOOLEAN NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_marketplace_currency CHECK (length(currency) = 3),
	CONSTRAINT uq_store_marketplace_code UNIQUE (store_id, code),
	CONSTRAINT ck_v1_marketplace_code CHECK (code IN ('CA', 'UK', 'AU', 'DE')),
	FOREIGN KEY(store_id) REFERENCES stores (id) ON DELETE CASCADE
);
CREATE TABLE "stores" (
	id INTEGER NOT NULL,
	account_id INTEGER,
	name VARCHAR(160) NOT NULL,
	selector_type VARCHAR(12) NOT NULL,
	selector_value VARCHAR(255) NOT NULL,
	browser_oauth VARCHAR(255),
	browser_id VARCHAR(80),
	expected_seller_id VARCHAR(120),
	identity_confirmed BOOLEAN NOT NULL,
	enabled BOOLEAN NOT NULL,
	last_seen_at DATETIME,
	raw_profile JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_store_selector_type CHECK (selector_type IN ('oauth','id')),
	UNIQUE (browser_oauth),
	FOREIGN KEY(account_id) REFERENCES ziniao_accounts (id) ON DELETE SET NULL,
	UNIQUE (selector_value)
);
CREATE TABLE system_settings (
	"key" VARCHAR(120) NOT NULL,
	value JSON NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY ("key")
);
CREATE TABLE ziniao_accounts (
	id INTEGER NOT NULL,
	display_name VARCHAR(120) NOT NULL,
	company VARCHAR(180),
	username VARCHAR(180),
	credential_ref VARCHAR(255),
	enabled BOOLEAN NOT NULL,
	last_synced_at DATETIME,
	sync_error TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (credential_ref)
);
CREATE INDEX ix_admin_sessions_admin_id ON admin_sessions (admin_id);
CREATE INDEX ix_admin_sessions_expires_at ON admin_sessions (expires_at);
CREATE INDEX ix_approval_pending ON approval_requests (status, expires_at);
CREATE INDEX ix_approval_requests_run_id ON approval_requests (run_id);
CREATE INDEX ix_evidence_created_at ON evidence (created_at);
CREATE INDEX ix_evidence_run_id ON evidence (run_id);
CREATE INDEX ix_evidence_site_run_id ON evidence (site_run_id);
CREATE INDEX ix_feedback_reviews_run_id ON feedback_reviews (run_id);
CREATE INDEX ix_feedback_reviews_state ON feedback_reviews (state);
CREATE INDEX ix_feedback_reviews_store_id ON feedback_reviews (store_id);
CREATE INDEX ix_notification_deliveries_run_id ON notification_deliveries (run_id);
CREATE INDEX ix_notification_deliveries_site_run_id ON notification_deliveries (site_run_id);
CREATE INDEX ix_notification_delivery_status ON notification_deliveries (status, created_at);
CREATE INDEX ix_operation_guards_run_id ON operation_guards (run_id);
CREATE INDEX ix_operation_guards_site_run_id ON operation_guards (site_run_id);
CREATE INDEX ix_operation_guards_state ON operation_guards (state);
CREATE INDEX ix_run_events_created_at ON run_events (created_at);
CREATE INDEX ix_run_events_run_id ON run_events (run_id);
CREATE INDEX ix_run_events_site_run_id ON run_events (site_run_id);
CREATE INDEX ix_run_queue_ready_order ON run_queue_entries (state, priority, scheduled_for_at, business_priority, enqueued_at, batch_scope_id, target_order, id);
CREATE INDEX ix_runs_schedule_active ON runs (schedule_id, status);
CREATE INDEX ix_runs_schedule_id ON runs (schedule_id);
CREATE INDEX ix_runs_status ON runs (status);
CREATE INDEX ix_runs_store_id ON runs (store_id);
CREATE INDEX ix_schedule_batches_created_at ON schedule_batches (created_at);
CREATE INDEX ix_schedules_batch_id ON schedules (batch_id);
CREATE INDEX ix_schedules_store_id ON schedules (store_id);
CREATE INDEX ix_site_runs_run_id ON site_runs (run_id);
CREATE INDEX ix_store_marketplaces_store_id ON store_marketplaces (store_id);
CREATE INDEX ix_stores_account_id ON stores (account_id);
CREATE UNIQUE INDEX uq_run_queue_active_run ON run_queue_entries (run_id) WHERE state IN ('READY','CLAIMED');
CREATE UNIQUE INDEX uq_runs_schedule_occurrence ON runs (schedule_id, scheduled_for_at) WHERE schedule_id IS NOT NULL AND scheduled_for_at IS NOT NULL;
INSERT INTO alembic_version(version_num) VALUES ('0010');
