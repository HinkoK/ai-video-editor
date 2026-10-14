-- Громкость ролика: цель в LUFS для экспорта (одно общее усиление);
-- NULL — не выравнивать. Стартовое значение −16.
ALTER TABLE projects ADD COLUMN loudness_lufs REAL DEFAULT -16.0
    CHECK (loudness_lufs IS NULL OR loudness_lufs BETWEEN -30 AND -8);
