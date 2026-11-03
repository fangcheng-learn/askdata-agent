-- Private schema, independent of application/business tables. Run once as owner.
CREATE SCHEMA IF NOT EXISTS insightpilot_trial;
REVOKE ALL ON SCHEMA insightpilot_trial FROM PUBLIC;
CREATE TABLE IF NOT EXISTS insightpilot_trial.gate (
  id integer PRIMARY KEY CHECK (id = 1), run_id text, lease_until timestamptz
);
INSERT INTO insightpilot_trial.gate(id) VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS insightpilot_trial.daily (
  day date PRIMARY KEY, reserved_microyuan bigint NOT NULL DEFAULT 0
    CHECK (reserved_microyuan >= 0)
);
CREATE TABLE IF NOT EXISTS insightpilot_trial.visitors (
  day date NOT NULL, visitor_id text NOT NULL, starts integer NOT NULL DEFAULT 0,
  PRIMARY KEY(day, visitor_id)
);
CREATE TABLE IF NOT EXISTS insightpilot_trial.reservations (
  id text PRIMARY KEY, day date NOT NULL REFERENCES insightpilot_trial.daily(day),
  run_id text NOT NULL, amount bigint NOT NULL CHECK (amount >= 0),
  settled boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE insightpilot_trial.gate ENABLE ROW LEVEL SECURITY;
ALTER TABLE insightpilot_trial.daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE insightpilot_trial.visitors ENABLE ROW LEVEL SECURITY;
ALTER TABLE insightpilot_trial.reservations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ALL TABLES IN SCHEMA insightpilot_trial FROM PUBLIC;
