-- Причина правки «ожидание»: долгое ожидание результата (команда работает,
-- модель думает) сокращается, начало и конец остаются. Решение автора от
-- 2026-10-07. SQLite не меняет CHECK у существующей таблицы, поэтому
-- proposal_ops пересобирается с теми же столбцами и данными.

CREATE TABLE proposal_ops_new (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals (id),
    position INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('remove', 'restore')),
    asset_id TEXT NOT NULL REFERENCES assets (id),
    -- Уже выровнено по кадрам: именно эти границы и будут исполнены
    src_start_us INTEGER NOT NULL,
    src_end_us INTEGER NOT NULL CHECK (src_end_us > src_start_us),
    reason TEXT NOT NULL CHECK (reason IN (
        'failed_take', 'false_start', 'restart_gap', 'technical_dead_air',
        'recording_setup', 'waiting', 'other', 'restore'
    )),
    why TEXT NOT NULL,
    kept_alt_json TEXT,
    segment_ids_json TEXT NOT NULL DEFAULT '[]',
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    decision TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'accepted', 'excluded')),
    override_reason TEXT,
    requested_start_us INTEGER,
    requested_end_us INTEGER,
    adjust_note TEXT,
    review TEXT NOT NULL DEFAULT 'pending' CHECK (review IN ('pending', 'kept', 'reverted', 'held')),
    UNIQUE (proposal_id, position)
);

INSERT INTO proposal_ops_new (
    id, proposal_id, position, kind, asset_id, src_start_us, src_end_us, reason, why, kept_alt_json,
    segment_ids_json, risk_flags_json, decision, override_reason, requested_start_us, requested_end_us,
    adjust_note, review
)
SELECT
    id, proposal_id, position, kind, asset_id, src_start_us, src_end_us, reason, why, kept_alt_json,
    segment_ids_json, risk_flags_json, decision, override_reason, requested_start_us, requested_end_us,
    adjust_note, review
FROM proposal_ops;

DROP TABLE proposal_ops;
ALTER TABLE proposal_ops_new RENAME TO proposal_ops;
CREATE INDEX ops_by_proposal ON proposal_ops (proposal_id);
