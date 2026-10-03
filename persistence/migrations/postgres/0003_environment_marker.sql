-- Labels a database with the environment it belongs to, so a staging server can never run against
-- production data (or the reverse). The label is written by `python -m persistence migrate` and checked at startup.

CREATE TABLE hub_environment (
    singleton   BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    name        TEXT NOT NULL CHECK (name IN ('development', 'staging', 'production')),
    labelled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
