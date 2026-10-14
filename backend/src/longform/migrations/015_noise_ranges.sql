-- Кашель ищется только в том, что осталось в версии (сначала чистка, потом
-- поиск): какие отрезки записи уже проверены и
-- каким правилом поиска. NULL — старый поиск по всей записи, его правило
-- устарело: такие проверяются заново.
ALTER TABLE noise_scans ADD COLUMN ranges_json TEXT;
ALTER TABLE noise_scans ADD COLUMN version INTEGER;
