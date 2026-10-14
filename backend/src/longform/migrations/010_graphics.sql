-- Моушн-графика: подписи, термины, схемы,
-- сравнения, чек-листы, команды крупно — поверх кадра. Привязка ко времени
-- записи экрана, как у участков кадра: переживает правки чистки.
-- Claude ставит с целью для зрителя; в режиме «сразу» действует сразу,
-- Автор проверяет: kept — оставил, reverted — вернул как было.
CREATE TABLE graphics (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    asset_id TEXT NOT NULL REFERENCES assets (id),
    src_start_us INTEGER NOT NULL,
    src_end_us INTEGER NOT NULL CHECK (src_end_us > src_start_us),
    template TEXT NOT NULL,
    props_json TEXT NOT NULL,
    goal TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
    review TEXT NOT NULL DEFAULT 'pending' CHECK (review IN ('pending', 'kept', 'reverted')),
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX graphics_by_project ON graphics (project_id, asset_id, src_start_us);

-- Пометка «ожидание сокращено» у сокращённых ожиданий — по умолчанию да
ALTER TABLE projects ADD COLUMN mark_waiting INTEGER NOT NULL DEFAULT 1;
