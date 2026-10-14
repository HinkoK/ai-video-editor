// Снимки экранов для сверки с макетом (ширина 1440, как в дизайне).
//
//   node scripts/shot.mjs <рабочая папка> <папка для снимков> [раздел ...]
//
// Разделы — подписи вкладок: Проверка, Расшифровка, Таймлайн, Задачи,
// Экспорт, Проект. Без разделов — все. Нужен запущенный longform-server
// с этой рабочей папкой; ключ берётся из .run/session.json и не печатается.
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import puppeteer from 'puppeteer-core'

const [workspace, out, ...wanted] = process.argv.slice(2)
if (!workspace || !out) {
  console.error('использование: node scripts/shot.mjs <рабочая папка> <папка для снимков> [раздел ...]')
  process.exit(2)
}
const tabs = wanted.length ? wanted : ['Проверка', 'Расшифровка', 'Таймлайн', 'Задачи', 'Экспорт', 'Проект']
const session = JSON.parse(readFileSync(join(workspace, '.run', 'session.json'), 'utf8'))
const chrome = process.env.CHROME ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
mkdirSync(out, { recursive: true })

const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-first-run'] })
try {
  const page = await browser.newPage()
  await page.setViewport({ width: Number(process.env.WIDTH ?? 1440), height: Number(process.env.HEIGHT ?? 1000) })
  page.on('pageerror', (e) => console.log(`ошибка страницы: ${e.message}`))
  page.on('console', (m) => m.type() === 'error' && console.log(`консоль: ${m.text()}`))
  await page.goto(`http://127.0.0.1:${session.port}/#key=${session.user_token}`, { waitUntil: 'networkidle0' })
  for (const tab of tabs) {
    await page.evaluate((label) => {
      const b = [...document.querySelectorAll('nav[aria-label="Разделы"] button')]
        .find((el) => el.firstChild?.textContent?.trim() === label)
      b?.click()
    }, tab)
    await page.waitForNetworkIdle({ idleTime: 500, timeout: 20000 }).catch(() => undefined)
    await new Promise((r) => setTimeout(r, 600))
    const path = join(out, `${tab}.png`)
    await page.screenshot({ path })
    console.log(path)
  }
} finally {
  await browser.close()
}
