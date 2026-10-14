-- Фоновые задачи и отчёты качества.

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    type TEXT NOT NULL CHECK (type IN ('proxy', 'preview', 'export', 'analyze', 'transcribe')),
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

CREATE INDEX jobs_queue ON jobs (state, priority, created_at);
CREATE INDEX jobs_by_hash ON jobs (params_hash, state);
CREATE INDEX jobs_by_project ON jobs (project_id, created_at);

CREATE TABLE qa_reports (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    revision_id TEXT NOT NULL REFERENCES revisions (id),
    job_id TEXT NOT NULL REFERENCES jobs (id),
    -- Три статуса не подменяют друг друга: проверки кода, замечания
    -- модели и проверка человеком
    technical_json TEXT NOT NULL,
    critical INTEGER NOT NULL,
    model_notes_json TEXT NOT NULL DEFAULT '[]',
    human_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (human_status IN ('pending', 'approved', 'changes_requested')),
    human_note TEXT,
    human_at TEXT,
    created_at TEXT NOT NULL
);

-- Один исполнитель: его pid и последний сигнал «я жив»
CREATE TABLE worker_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pid INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);
