-- Перестановка кусков (решение автора от 2026-10-13, этап step-29): кусок
-- ролика ставится в другое место. Версия — оставленные кадры (clips_json,
-- единственная правда для сборки) плюс упорядоченный слой перестановок
-- moves_json. Правки move (переставить) и unmove («вернуть как было» в
-- проверке) с причиной reorder; у move — место: после или перед моментом
-- исходника. SQLite не меняет CHECK у существующей таблицы — proposal_ops
-- пересобирается, как в 005 и 013.

CREATE TABLE proposal_ops_new (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals (id),
    position INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('remove', 'restore', 'move', 'unmove')),
    asset_id TEXT NOT NULL REFERENCES assets (id),
    -- Уже выровнено по кадрам: именно эти границы и будут исполнены
    src_start_us INTEGER NOT NULL,
    src_end_us INTEGER NOT NULL CHECK (src_end_us > src_start_us),
    reason TEXT NOT NULL CHECK (reason IN (
        'failed_take', 'false_start', 'restart_gap', 'technical_dead_air',
        'recording_setup', 'waiting', 'vocal_noise', 'other', 'restore', 'reorder'
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
    -- Перестановка: кусок встаёт после кадра, который кончается в target_src_us
    -- (after), или перед кадром, который в этот момент начинается (before)
    target_asset_id TEXT REFERENCES assets (id),
    target_src_us INTEGER,
    target_side TEXT CHECK (target_side IN ('before', 'after')),
    -- Снятие перестановки: id правки move, которую снимает unmove
    undo_of TEXT,
    UNIQUE (proposal_id, position),
    CHECK ((kind = 'move') = (target_src_us IS NOT NULL)),
    CHECK (kind != 'move' OR (target_asset_id IS NOT NULL AND target_side IS NOT NULL)),
    CHECK ((kind = 'unmove') = (undo_of IS NOT NULL)),
    CHECK ((kind IN ('move', 'unmove')) = (reason = 'reorder'))
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

-- Слой перестановок версии: [{op_id, asset_id, src_start_us, src_end_us,
-- target_asset_id, target_src_us, side}] по порядку применения
ALTER TABLE revisions ADD COLUMN moves_json TEXT NOT NULL DEFAULT '[]';

-- Версия неизменна [R-005]: слой перестановок — тоже содержимое версии
DROP TRIGGER revisions_content_immutable;
CREATE TRIGGER revisions_content_immutable
BEFORE UPDATE OF clips_json, moves_json, project_id, parent_id, number, created_by ON revisions
BEGIN
    SELECT RAISE(ABORT, 'содержимое версии менять нельзя');
END;
