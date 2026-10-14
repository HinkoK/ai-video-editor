-- Снимки страниц для вставок в ролик (шаблон page_insert):
-- Claude снимает страницу по ссылке (Chrome без окна, интернет только для
-- снимка), автор может подложить свой снимок файлом — например страницу,
-- закрытую входом. Картинка лежит в папке проекта pages/<id>.png.
CREATE TABLE pages (
    id TEXT NOT NULL,              -- первые 16 знаков sha256 картинки
    project_id TEXT NOT NULL REFERENCES projects (id),
    url TEXT,                      -- откуда снята; NULL — файл автора
    title TEXT NOT NULL DEFAULT '',
    width INTEGER NOT NULL CHECK (width > 0),   -- пиксели снимка
    height INTEGER NOT NULL CHECK (height > 0),
    bg TEXT NOT NULL DEFAULT '#ffffff',  -- цвет фона страницы: им закрашиваются поля рамки
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);
