-- Кашель и «кхм» (решение автора от 2026-10-07: «перед тем как говорить, я
-- кашляю, делаю так: кхм — убирай из всех видео»). Новая причина правки
-- vocal_noise и найденные звуки голоса без слов (longform.noises). SQLite не
-- меняет CHECK у существующей таблицы — proposal_ops пересобирается, как в 005.

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
        'recording_setup', 'waiting', 'vocal_noise', 'other', 'restore'
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

-- Звуки голоса без слов по записи экрана: сам звук и что вырезать (звук и
-- тишина вокруг до запаса у речи); heard — что Whisper услышал в нём отдельно
CREATE TABLE noises (
    asset_id TEXT NOT NULL REFERENCES assets (id),
    idx INTEGER NOT NULL,
    start_us INTEGER NOT NULL,
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    cut_start_us INTEGER NOT NULL,
    cut_end_us INTEGER NOT NULL CHECK (cut_end_us > cut_start_us),
    heard TEXT NOT NULL DEFAULT '',
    peak_db REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, idx)
);

-- Когда искали: пустой список после поиска — «кашля нет», а не «ещё не искали»
CREATE TABLE noise_scans (
    asset_id TEXT PRIMARY KEY REFERENCES assets (id),
    transcript_id TEXT,
    found INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
