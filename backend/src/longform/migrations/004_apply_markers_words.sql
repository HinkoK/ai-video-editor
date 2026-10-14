-- Правки Claude применяются сразу, автор проверяет после; метки; время слов;
-- детектор речи.

-- apply — правки агента сразу становятся текущей версией (кроме тех, что
-- задевают жёсткую защиту); propose — ждут утверждения, как раньше.
-- Менять может только автор.
ALTER TABLE projects ADD COLUMN agent_mode TEXT NOT NULL DEFAULT 'apply'
    CHECK (agent_mode IN ('apply', 'propose'));

ALTER TABLE proposals ADD COLUMN auto_applied INTEGER NOT NULL DEFAULT 0;

-- Проверка после применения: pending — автор ещё не смотрел, kept — оставил,
-- reverted — вернул, held — не применена (задевает жёсткую защиту) и ждёт
ALTER TABLE proposal_ops ADD COLUMN review TEXT NOT NULL DEFAULT 'pending'
    CHECK (review IN ('pending', 'kept', 'reverted', 'held'));

-- Нумерованные метки: общий язык «между 3-й и 4-й меткой». Привязка — ко
-- времени исходника, как у комментариев, поэтому метка переживает правки.
CREATE TABLE markers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    number INTEGER NOT NULL,
    asset_id TEXT NOT NULL REFERENCES assets (id),
    src_us INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
    created_at TEXT NOT NULL,
    removed_at TEXT,
    UNIQUE (project_id, number)
);

-- Время слов по DTW whisper.cpp: приблизительное (на синтетике 0–230 мс от
-- начала речи), не проверено на голосе автора
CREATE TABLE transcript_words (
    segment_id TEXT NOT NULL REFERENCES transcript_segments (id),
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    start_us INTEGER NOT NULL,
    end_us INTEGER NOT NULL,
    p REAL,
    PRIMARY KEY (segment_id, idx)
);

-- Отрезки речи по детектору Silero: речь отличается от стука клавиатуры
-- и шума, которые по одной громкости выглядят как речь
ALTER TABLE audio_analysis ADD COLUMN vad_json TEXT;
