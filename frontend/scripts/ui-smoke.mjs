// Сквозная проверка интерфейса в настоящем Chrome (без окна).
//
//   node scripts/ui-smoke.mjs <рабочая папка> [папка для снимков]
//
// Нужен запущенный longform-server с проектом в режиме «Claude правит сразу»,
// где не меньше двух непроверенных правок Claude (подходит пробный проект
// backend/scripts/demo_workspace.py). Сценарий оставляет одну и возвращает
// другую — данные проекта меняются. Ключ берётся из
// <рабочая папка>/.run/session.json и в вывод не попадает.
import { readFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import puppeteer from 'puppeteer-core'

const [workspace, shotsDir = 'ui-shots'] = process.argv.slice(2)
if (!workspace) {
  console.error('использование: node scripts/ui-smoke.mjs <рабочая папка> [папка для снимков]')
  process.exit(2)
}
const session = JSON.parse(readFileSync(join(workspace, '.run', 'session.json'), 'utf8'))
const base = `http://127.0.0.1:${session.port}`
const chrome = process.env.CHROME ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
mkdirSync(shotsDir, { recursive: true })

const problems = []
const passed = []
const check = (ok, what) => (ok ? passed : problems).push(what)

const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-first-run'] })
try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1280, height: 900 })
  // «Failed to load resource» дублирует ответ сервера, его ловит обработчик ответов
  page.on('console', (m) => m.type() === 'error' && !m.text().startsWith('Failed to load resource') &&
    problems.push(`консоль: ${m.text()}`))
  page.on('pageerror', (e) => problems.push(`ошибка страницы: ${e.message}`))
  page.on('response', (r) => r.status() >= 400 && problems.push(`ответ ${r.status()}: ${new URL(r.url()).pathname}`))
  // Полоса кадров дорожки видео: браузер берёт её один раз, дальше из памяти
  const strips = []
  page.on('response', (r) => r.url().includes('/thumbs.jpg') && strips.push(r.status()))

  const text = () => page.evaluate(() => document.body.innerText)
  const waitText = (t, timeout = 15000) =>
    page.waitForFunction((s) => document.body.innerText.includes(s), { timeout }, t)
  // Подпись кнопки без подсказки клавиши (<kbd>) внутри
  const clickButton = async (label) => {
    const buttons = await page.$$('button')
    for (const b of buttons) {
      const own = await b.evaluate((el) =>
        [...el.childNodes].filter((n) => n.nodeName !== 'KBD').map((n) => n.textContent).join('').trim())
      if (own === label) {
        await b.click()
        return true
      }
    }
    throw new Error(`нет кнопки «${label}»`)
  }
  const shot = (name) => page.screenshot({ path: join(shotsDir, `${name}.png`), fullPage: true })

  await page.goto(`${base}/#key=${session.user_token}`, { waitUntil: 'networkidle0' })
  check(!page.url().includes('key='), 'ключ стёрт из адреса после входа')
  check(!(await page.evaluate(() => document.cookie)).includes('longform_session'), 'cookie недоступна скриптам (HttpOnly)')

  // Режим «Claude правит сразу»: его правки уже применены, проверяем после
  await page.waitForSelector('aside[aria-label="Очередь правок"]', { timeout: 15000 })
  await waitText('Claude правит сразу')
  check(true, 'экран проверки открылся первым и показал очередь правок Claude')
  const unreviewed = async () => Number((await text()).match(/Не проверено правок: (\d+)/)?.[1])
  // Счётчик дошёл до n; при нуле строка меняется на «Все … проверены» (если нет ждущих решения)
  const waitUnreviewed = (n) => page.waitForFunction((n) => {
    const t = document.body.innerText
    return new RegExp(`Не проверено правок: ${n}\\D`).test(t) || (n === 0 && /Все \d+ правк\S* проверены|Правка проверена/.test(t))
  }, { timeout: 15000 }, n)
  const n0 = await unreviewed()
  check(n0 >= 2, `показано, сколько правок не проверено (${n0}; для сценария нужно не меньше 2)`)

  // Первая непроверенная правка в очереди: текст вокруг показан сразу
  await page.click('li[data-review="pending"] [data-queue-item]')
  await page.waitForSelector('li[data-removed="yes"]', { timeout: 15000 })
  check((await text()).includes('уходит'), 'текст вокруг правки показан, вырезанное помечено')
  await shot('1-проверка')

  await clickButton('Оставить')
  await waitUnreviewed(n0 - 1)
  check(true, 'правку можно оставить, счётчик непроверенного обновился')
  // Оставленная свёрнута в «Проверены» внизу очереди, а не висит в списке
  const folded = await page.waitForSelector('.rv-done-toggle[aria-expanded="false"]', { timeout: 5000 })
    .then(() => true, () => false)
  check(folded && !(await text()).includes('оставлена'), 'проверенное свёрнуто в «Проверены»')
  // После решения выбрана следующая непроверенная — её и возвращаем
  await clickButton('Вернуть как было')
  await waitUnreviewed(n0 - 2)
  check(true, 'правку можно вернуть, счётчик непроверенного обновился')
  // Свёрнутое открывается (когда проверять больше нечего, оно уже открыто)
  if (await page.$('.rv-done-toggle')) await page.click('.rv-done-toggle')
  await waitText('возвращена')
  check((await text()).includes('оставлена'), 'в «Проверены» видно: одна оставлена, другая возвращена')
  await shot('2-после-проверки')

  const tabs = [
    ['Расшифровка', 'Фраз:'],
    ['Таймлайн', 'целиком'],
    ['Задачи', 'Исполнитель'],
    ['Экспорт', 'Экспортировать текущую версию'],
    ['Проект', 'Как Claude вносит правки'],
  ]
  for (const [tab, marker] of tabs) {
    await clickButton(tab)
    await waitText(marker)
    check(true, `вкладка «${tab}» открылась`)
    await shot(`3-${tab}`)
  }

  await clickButton('Расшифровка')
  await waitText('Фраз:')
  await page.type('input[type="search"]', 'ДОЛЛАР')
  await page.keyboard.press('Enter')
  await page.waitForFunction(
    () => document.querySelector('[aria-label="Найдено"]')?.innerText.includes('Стоимость одного часа'),
    { timeout: 15000 },
  )
  check(true, 'поиск по расшифровке без учёта регистра')
  check((await page.$$('canvas')).length === 0, 'на вкладке расшифровки нет холста таймлайна')

  await clickButton('Таймлайн')
  await waitText('целиком')
  // Над таймлайном — монитор программы со своим холстом (.mon-canvas); сам таймлайн — один холст
  check((await page.$$('canvas:not(.mon-canvas)')).length === 1, 'таймлайн нарисован на одном холсте')
  await page.waitForNetworkIdle({ idleTime: 300, timeout: 15000 }).catch(() => undefined)
  check(strips.length > 0 && strips.every((s) => s === 200), 'дорожка видео: полоса кадров загрузилась')
  const marks = () => page.$$eval('.tl-marks li', (els) => els.length)
  const before = await marks()
  await page.focus('canvas')
  await page.keyboard.press('KeyM')
  await page.waitForFunction((n) => document.querySelectorAll('.tl-marks li').length === n + 1, { timeout: 10000 }, before)
  check(true, 'клавиша M поставила метку')

  await clickButton('Проект')
  await waitText('Хранилище')
  // Когда места мало, вместо «Свободно на диске» — предупреждение «Мало места на диске»
  await page.waitForFunction(() => /Свободно на диске|Мало места на диске/.test(document.body.innerText), { timeout: 15000 })
  // Названия категорий в макете с заглавной: «Готовые ролики»
  check(/готовые ролики/i.test(await text()), 'хранилище показывает свободное место и категории')
} catch (e) {
  problems.push(`сценарий прерван: ${e.message}`)
} finally {
  await browser.close()
}

for (const p of passed) console.log(`✓ ${p}`)
for (const p of problems) console.log(`✗ ${p}`)
console.log(`\nпройдено ${passed.length}, проблем ${problems.length}; снимки в ${shotsDir}`)
process.exit(problems.length ? 1 : 0)
