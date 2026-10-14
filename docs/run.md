# Запуск и проверки

## Что нужно

- macOS, ffmpeg и ffprobe в PATH.
- whisper.cpp (`whisper-cli` в PATH), модели
  `~/.cache/whisper/ggml-large-v3-q5_0.bin` и Silero VAD
  `~/.cache/whisper/ggml-silero-v6.2.0.bin` (другие пути —
  `LONGFORM_WHISPER_MODEL`, `LONGFORM_VAD_MODEL`).
- [uv](https://docs.astral.sh/uv/) (Python 3.12 он поставит сам), Node 22.
- Google Chrome (другой путь — `CHROME`).
- Claude Code — для монтажёра.

## Команды

    cd frontend && npm install && npm run build      # интерфейс, один раз после изменений
    cd graphics && npm install                       # графика, один раз
    cd backend && uv sync
    cd backend && uv run longform-server             # сервер + фоновый исполнитель
    cd backend && uv run longform-open               # открыть редактор в браузере
    cd backend && uv run longform-agent <id проекта> # рабочая папка Claude с .mcp.json и запретами
    <папка Claude>/start.sh                          # Claude Code без встроенных инструментов

Переменные:

| Переменная | Что | По умолчанию |
|---|---|---|
| `LONGFORM_WORKSPACE` | рабочая папка: база, медиа, ключи | `~/Movies/AI_Video_Editor_Media` |
| `LONGFORM_PORT` | порт сервера (только 127.0.0.1) | 8765 |
| `LONGFORM_STYLE` | папка стиля автора для Claude | `style/` в корне репозитория |
| `LONGFORM_DEV_ORIGIN` | адрес dev-сервера интерфейса | — |
| `CHROME` | путь к Google Chrome | установленный Chrome |

Разработка интерфейса: `LONGFORM_DEV_ORIGIN=http://localhost:5173 uv run longform-server`
и `cd frontend && npm run dev`.

Пробный проект без своей записи:

    cd backend && uv run python scripts/demo_workspace.py <пустая папка>
    LONGFORM_WORKSPACE=<папка>/рабочая uv run longform-server

Остальные проверки и замеры:

    cd frontend && node scripts/ui-smoke.mjs <рабочая папка>   # интерфейс в Chrome
    cd frontend && node scripts/shot.mjs <рабочая папка> <папка снимков> [раздел]   # снимки 1440 px
    cd backend && uv run python scripts/spike_render.py --fps 30000/1001 --cuts 200   # замер склейки
    cd backend && uv run python scripts/eval_agent.py ...            # оценка Claude по разметке (docs/eval/)
    cd backend && uv run python scripts/longform_gate.py <файлы>     # проверка на длинной записи
    node graphics/stills.mjs <примеры.json> <папка>                  # кадры графики глазами

## Обязательные проверки

- `cd backend && uv run pytest` — тесты сервера, около 4 минут.
- `cd frontend && npx vitest run && npm run build` — тесты и сборка интерфейса.

По тому, что задели: интерфейс — живой сервер и браузер; графика —
`fitcheck.mjs` (тест `test_templates_fit_the_frame`) и кадры глазами; сборка —
пробный ролик и отчёт экспорта (кадры, отсчёты, совпадение с версией).

Тесты падают с `DiskSpaceError`, если на диске меньше 5 ГБ запаса: сначала
освободи место, а не снижай запас.

## Ключи

Ключи доступа создаются при каждом запуске сервера в
`<рабочая папка>/.run/session.json` (права 600): ключ автора даёт всё, ключ
Claude — чтение, предложения, мягкую защиту, черновик брифа, комментарии и
пробные ролики. Браузер получает ключ во фрагменте адреса и сразу меняет его
на HttpOnly cookie — если снимаешь экран, открой редактор до записи.
