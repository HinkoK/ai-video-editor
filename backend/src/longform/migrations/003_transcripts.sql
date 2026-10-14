-- Анализ звука и расшифровка.

CREATE TABLE audio_analysis (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets (id),
    params_hash TEXT NOT NULL,
    hop_us INTEGER NOT NULL,  -- шаг измерения громкости
    floor_db REAL NOT NULL,  -- уровень шума этой записи
    speech_db REAL NOT NULL,  -- типичный уровень речи
    threshold_db REAL NOT NULL,  -- ниже — тишина
    -- Файл с громкостью (дБ на каждый шаг); время 0 — первый отсчёт звука
    path TEXT NOT NULL,
    audio_offset_us INTEGER NOT NULL,  -- звук начинается на столько позже первого кадра
    created_at TEXT NOT NULL,
    UNIQUE (asset_id, params_hash)
);

CREATE TABLE silences (
    analysis_id TEXT NOT NULL REFERENCES audio_analysis (id),
    asset_id TEXT NOT NULL REFERENCES assets (id),
    src_start_us INTEGER NOT NULL,
    src_end_us INTEGER NOT NULL,
    min_db REAL NOT NULL
);
CREATE INDEX silences_by_asset ON silences (asset_id, src_start_us);

CREATE TABLE transcripts (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets (id),
    params_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    language TEXT NOT NULL,
    -- Честная точность времени: 'segment' — только границы фраз, и те
    -- приблизительные. Резать по словам при таком уровне нельзя.
    timing_level TEXT NOT NULL CHECK (timing_level IN ('segment', 'word_verified')),
    created_at TEXT NOT NULL,
    UNIQUE (asset_id, params_hash)
);

CREATE TABLE transcript_segments (
    id TEXT PRIMARY KEY,
    transcript_id TEXT NOT NULL REFERENCES transcripts (id),
    asset_id TEXT NOT NULL REFERENCES assets (id),
    idx INTEGER NOT NULL,
    -- Границы, которые дал Whisper (часто захватывают паузы)
    src_start_us INTEGER NOT NULL,
    src_end_us INTEGER NOT NULL,
    -- Где внутри сегмента реально звучит речь (по громкости); NULL — речи нет
    speech_start_us INTEGER,
    speech_end_us INTEGER,
    text_raw TEXT NOT NULL,
    text_corrected TEXT,  -- исправление автора, сырой текст не перезаписывается
    avg_p REAL,
    flags_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (transcript_id, idx)
);
CREATE INDEX segments_by_asset ON transcript_segments (asset_id, src_start_us);

-- Поиск по тексту; unicode61 приводит кириллицу к одному регистру
CREATE VIRTUAL TABLE segments_fts USING fts5 (segment_id UNINDEXED, text, tokenize = 'unicode61');

-- Правки запоминают, что попросил агент, если сервер сдвинул границы
ALTER TABLE proposal_ops ADD COLUMN requested_start_us INTEGER;
ALTER TABLE proposal_ops ADD COLUMN requested_end_us INTEGER;
ALTER TABLE proposal_ops ADD COLUMN adjust_note TEXT;
