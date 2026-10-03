-- Indexes for authorization, record-kind, deleted/archive, relationship, reminder and date queries.

CREATE INDEX idx_records_kind_active ON records (kind, id) WHERE NOT deleted;
CREATE INDEX idx_records_visibility ON records ((body->>'visibility')) WHERE NOT deleted;
CREATE INDEX idx_records_creator ON records ((body->>'creator')) WHERE NOT deleted;
CREATE INDEX idx_records_who ON records ((body->>'who')) WHERE NOT deleted;
CREATE INDEX idx_records_space ON records ((body->>'space')) WHERE kind = 'notes';

CREATE INDEX idx_records_dance_type ON records ((body->>'danceType')) WHERE kind = 'dance';
CREATE INDEX idx_records_dance_archived ON records ((body->>'archived')) WHERE kind = 'dance';
CREATE INDEX idx_records_dance_links ON records USING GIN (body jsonb_path_ops) WHERE kind = 'dance';

CREATE INDEX idx_records_source_dance ON records ((body->>'sourceDanceId')) WHERE body->>'sourceDanceId' IS NOT NULL;
CREATE INDEX idx_records_source_activity ON records ((body->>'sourceActivityId')) WHERE body->>'sourceActivityId' IS NOT NULL;
CREATE INDEX idx_records_series ON records ((body->>'seriesId')) WHERE body->>'seriesId' IS NOT NULL;

CREATE INDEX idx_records_event_date ON records ((body->>'date')) WHERE kind = 'events';
CREATE INDEX idx_records_task_due ON records ((body->>'dueDate')) WHERE kind = 'tasks';

CREATE INDEX idx_audit_record ON audit (record_id, id DESC);
CREATE INDEX idx_audit_actor ON audit (actor);
CREATE INDEX idx_audit_created ON audit (created);

CREATE INDEX idx_reminders_account_due ON reminders (account, due_at);
CREATE INDEX idx_reminders_open ON reminders (account) WHERE read_at IS NULL AND dismissed_at IS NULL;
CREATE INDEX idx_reminders_source ON reminders (source_kind, source_id);

CREATE INDEX idx_occurrences_sequence ON recurrence_occurrences (series_id, sequence);
CREATE INDEX idx_receipts_recipient ON recognition_receipts (recipient);
CREATE INDEX idx_sessions_expires ON sessions (expires);
CREATE INDEX idx_sessions_name ON sessions (name);
