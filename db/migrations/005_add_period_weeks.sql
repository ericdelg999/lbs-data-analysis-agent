-- Migration 005: Add period_weeks to findings + reports tables
-- Rationale: report pipeline now supports configurable rolling windows.
-- Existing rows default to 1 (legacy weekly behavior).

ALTER TABLE findings
    ADD COLUMN IF NOT EXISTS period_weeks INTEGER NOT NULL DEFAULT 1;

ALTER TABLE reports
    ADD COLUMN IF NOT EXISTS period_weeks INTEGER NOT NULL DEFAULT 1;

UPDATE findings SET period_weeks = 1 WHERE period_weeks IS NULL;
UPDATE reports SET period_weeks = 1 WHERE period_weeks IS NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'reports'::regclass
          AND conname = 'reports_week_ending_key'
    ) THEN
        ALTER TABLE reports DROP CONSTRAINT reports_week_ending_key;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'reports'::regclass
          AND conname = 'reports_week_ending_period_weeks_key'
    ) THEN
        ALTER TABLE reports
            ADD CONSTRAINT reports_week_ending_period_weeks_key
            UNIQUE (week_ending, period_weeks);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_findings_period_week
    ON findings (week_ending, period_weeks);

CREATE INDEX IF NOT EXISTS idx_reports_period_week
    ON reports (week_ending, period_weeks);
