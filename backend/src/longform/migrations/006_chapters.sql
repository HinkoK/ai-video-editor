-- Главы для YouTube — это метки с названием: та же привязка ко времени
-- исходника, тот же номер M, но kind = 'chapter'.
ALTER TABLE markers ADD COLUMN kind TEXT NOT NULL DEFAULT 'mark' CHECK (kind IN ('mark', 'chapter'));
