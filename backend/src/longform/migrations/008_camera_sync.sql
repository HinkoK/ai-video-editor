-- Синхронная запись (решение автора от 2026-10-07): экран с микрофоном и
-- камера пишутся одновременно на разных устройствах.
-- role = 'main' — ведущий файл: в нём речь, по нему расшифровка и правки.
-- role = 'camera' — ведомый: картинка лица; звук только для синхронизации.
-- Время камеры: cam_us = sync_offset_us + main_us * (1 + sync_drift_ppm / 1e6).
ALTER TABLE assets ADD COLUMN role TEXT NOT NULL DEFAULT 'main' CHECK (role IN ('main', 'camera'));
ALTER TABLE assets ADD COLUMN sync_to TEXT REFERENCES assets (id);
ALTER TABLE assets ADD COLUMN sync_offset_us INTEGER;
ALTER TABLE assets ADD COLUMN sync_drift_ppm REAL;
-- pending — ещё не считали; ok — уверенно; weak — мало точек или разброс;
-- failed — не нашли; manual — автор подстроил вручную
ALTER TABLE assets ADD COLUMN sync_state TEXT CHECK (sync_state IN ('pending', 'ok', 'weak', 'failed', 'manual'));
-- Замеры по точкам: [{main_us, offset_us, peak}] и разброс — для проверки
ALTER TABLE assets ADD COLUMN sync_json TEXT;

-- Тип задачи sync: свести камеру по звуку. CHECK не меняется без пересборки
-- таблицы; на jobs ссылаются отчёты качества, поэтому:
-- foreign_keys: off
CREATE TABLE jobs_new (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    type TEXT NOT NULL CHECK (type IN ('proxy', 'preview', 'export', 'analyze', 'transcribe', 'sync')),
    -- Меньше — раньше: короткий пробный ролик не ждёт часового экспорта в очереди
    priority INTEGER NOT NULL,
    asset_id TEXT REFERENCES assets (id),
    revision_id TEXT REFERENCES revisions (id),
    params_json TEXT NOT NULL,
    -- Отпечаток всего, от чего зависит результат: тот же отпечаток — тот же файл
    params_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted')),
    attempts INTEGER NOT NULL DEFAULT 0,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    output_path TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker_pid INTEGER,
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    heartbeat_at TEXT
);
INSERT INTO jobs_new SELECT id, project_id, type, priority, asset_id, revision_id, params_json, params_hash, state,
    attempts, progress, message, output_path, error, cancel_requested, worker_pid, created_by, created_at,
    started_at, finished_at, heartbeat_at FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;
CREATE INDEX jobs_queue ON jobs (state, priority, created_at);
CREATE INDEX jobs_by_hash ON jobs (params_hash, state);
CREATE INDEX jobs_by_project ON jobs (project_id, created_at);
