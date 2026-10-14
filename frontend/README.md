# Интерфейс редактора

React + TypeScript + Vite. Оформление — в `src/styles/`, дизайн сделан
в Claude Design, что и куда встаёт — [../docs/design-handoff.md](../docs/design-handoff.md).

    npm install
    npm run build          # собирается в dist/, его раздаёт longform-server
    npm run dev            # разработка; сервер с LONGFORM_DEV_ORIGIN=http://localhost:5173
    npx vitest run         # тесты логики (время, очередь проверки)
    node scripts/ui-smoke.mjs <рабочая папка>   # сквозная проверка в установленном Chrome

Зависимости сверх шаблона Vite: `@tanstack/react-virtual` (виртуальный
список фраз: час речи — тысяча с лишним строк), `vitest` и `puppeteer-core`
(только для проверок; браузер не скачивает, берёт установленный Chrome).
