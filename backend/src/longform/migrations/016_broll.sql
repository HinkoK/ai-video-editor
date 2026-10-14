-- Свои кадры автора (решение 2026-10-08): broll — клип из папки broll рабочей
-- папки во весь кадр, голос идёт поверх (params: clip, at).
-- CHECK раскладки не меняется без пересборки таблицы (как в 011):
-- foreign_keys: off
CREATE TABLE visual_segments_new (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    asset_id TEXT NOT NULL REFERENCES assets (id),
    src_start_us INTEGER NOT NULL,
    src_end_us INTEGER NOT NULL CHECK (src_end_us > src_start_us),
    layout TEXT NOT NULL CHECK (layout IN ('screen', 'camera', 'pip', 'side', 'broll')),
    params_json TEXT NOT NULL DEFAULT '{}',
    goal TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
    review TEXT NOT NULL DEFAULT 'pending' CHECK (review IN ('pending', 'kept', 'reverted')),
    created_at TEXT NOT NULL,
    decided_at TEXT
);
INSERT INTO visual_segments_new SELECT * FROM visual_segments;
DROP TABLE visual_segments;
ALTER TABLE visual_segments_new RENAME TO visual_segments;
CREATE INDEX visual_by_project ON visual_segments (project_id, asset_id, src_start_us);
