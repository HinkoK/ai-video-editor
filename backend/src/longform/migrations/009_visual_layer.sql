-- Слой кадра: что видно в каждом месте ролика.
-- Участок привязан ко времени записи экрана (как комментарии и метки),
-- поэтому переживает правки чистки: вырезанная часть участка просто не видна.
--   screen — экран целиком (по желанию — приближение области: zoom)
--   camera — лицо целиком (по желанию — крупный план: crop)
--   pip    — экран и лицо в углу (slot, size)
-- Вне участков — раскладка проекта по умолчанию (projects.layout_json).
-- Claude предлагает участок с целью для зрителя; в режиме «сразу» он сразу
-- действует, автор проверяет: kept — оставил, reverted — вернул как было.
CREATE TABLE visual_segments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    asset_id TEXT NOT NULL REFERENCES assets (id),
    src_start_us INTEGER NOT NULL,
    src_end_us INTEGER NOT NULL CHECK (src_end_us > src_start_us),
    layout TEXT NOT NULL CHECK (layout IN ('screen', 'camera', 'pip')),
    params_json TEXT NOT NULL DEFAULT '{}',
    goal TEXT NOT NULL,  -- что зритель должен увидеть или понять
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
    review TEXT NOT NULL DEFAULT 'pending' CHECK (review IN ('pending', 'kept', 'reverted')),
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX visual_by_project ON visual_segments (project_id, asset_id, src_start_us);

-- Раскладка по умолчанию: {"layout": "pip", "pip": {"slot": "br", "size": 0.22}}.
-- NULL — экран с лицом в углу, если камера сведена, иначе экран.
ALTER TABLE projects ADD COLUMN layout_json TEXT;
