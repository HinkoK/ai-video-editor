-- Схема проекта монтажа. Время исходника в целых микросекундах,
-- частоты точными дробями (числитель и знаменатель отдельно).

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- Профиль ролика задаётся первым исходником, остальные должны совпасть
    fps_num INTEGER,
    fps_den INTEGER,
    sample_rate INTEGER,
    channels INTEGER,
    width INTEGER,
    height INTEGER,
    mode TEXT NOT NULL DEFAULT 'conservative_cleanup' CHECK (mode IN ('conservative_cleanup')),
    current_revision_id TEXT REFERENCES revisions (id)
);

CREATE TABLE assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    position INTEGER NOT NULL,  -- порядок файла в ролике
    path TEXT NOT NULL,  -- настоящий путь, ссылки уже раскрыты
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    fps_num INTEGER NOT NULL,
    fps_den INTEGER NOT NULL,
    frame_count INTEGER NOT NULL,
    sample_rate INTEGER NOT NULL,
    channels INTEGER NOT NULL,
    audio_offset_num INTEGER NOT NULL,
    audio_offset_den INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    registered_at TEXT NOT NULL,
    UNIQUE (project_id, path),
    UNIQUE (project_id, position)
);

CREATE TABLE revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    number INTEGER NOT NULL,
    parent_id TEXT REFERENCES revisions (id),
    state TEXT NOT NULL CHECK (state IN ('candidate', 'approved', 'discarded')),
    clips_json TEXT NOT NULL,
    description TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
    created_at TEXT NOT NULL,
    UNIQUE (project_id, number)
);

-- Версия неизменна: содержимое не правится, версии не удаляются,
-- состояние меняется только у кандидата (на «утверждена» или «отброшена»).
CREATE TRIGGER revisions_content_immutable
BEFORE UPDATE OF clips_json, project_id, parent_id, number, created_by ON revisions
BEGIN
    SELECT RAISE(ABORT, 'содержимое версии менять нельзя');
END;

CREATE TRIGGER revisions_state_forward_only
BEFORE UPDATE OF state ON revisions
WHEN OLD.state != 'candidate'
BEGIN
    SELECT RAISE(ABORT, 'состояние меняется только у кандидата');
END;

CREATE TRIGGER revisions_never_deleted
BEFORE DELETE ON revisions
BEGIN
    SELECT RAISE(ABORT, 'версии не удаляются');
END;

CREATE TABLE proposals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    number INTEGER NOT NULL,
    base_revision_id TEXT NOT NULL REFERENCES revisions (id),
    candidate_revision_id TEXT NOT NULL REFERENCES revisions (id),
    result_revision_id TEXT REFERENCES revisions (id),
    pass TEXT NOT NULL CHECK (pass IN ('cleanup')),
    rationale TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    notes_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('open', 'approved', 'rejected', 'stale')),
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE (project_id, number)
);

CREATE TABLE proposal_ops (
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
        'recording_setup', 'other', 'restore'
    )),
    why TEXT NOT NULL,
    kept_alt_json TEXT,
    segment_ids_json TEXT NOT NULL DEFAULT '[]',
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    decision TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'accepted', 'excluded')),
    override_reason TEXT,
    UNIQUE (proposal_id, position)
);

CREATE TABLE briefs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    version INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,  -- черновик агента действует только после подтверждения
    UNIQUE (project_id, version)
);

CREATE TABLE protected_ranges (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    asset_id TEXT NOT NULL REFERENCES assets (id),
    src_start_us INTEGER NOT NULL,
    src_end_us INTEGER NOT NULL CHECK (src_end_us > src_start_us),
    kind TEXT NOT NULL CHECK (kind IN ('hard', 'soft')),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent', 'system')),
    created_at TEXT NOT NULL,
    removed_at TEXT,
    -- Жёсткую защиту ставит только автор
    CHECK (kind = 'soft' OR created_by = 'user')
);

CREATE TABLE comments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    -- Версия, в которой оставлен комментарий. Привязка — ко времени
    -- исходника, поэтому комментарий переживает любые правки.
    revision_id TEXT NOT NULL REFERENCES revisions (id),
    asset_id TEXT NOT NULL REFERENCES assets (id),
    src_us INTEGER NOT NULL,
    src_end_us INTEGER,
    text TEXT NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('user', 'agent')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at TEXT NOT NULL,
    CHECK (src_end_us IS NULL OR src_end_us > src_us)
);

CREATE INDEX proposals_by_project ON proposals (project_id, status);
CREATE INDEX ops_by_proposal ON proposal_ops (proposal_id);
CREATE INDEX comments_by_project ON comments (project_id);
CREATE INDEX protections_by_project ON protected_ranges (project_id);
