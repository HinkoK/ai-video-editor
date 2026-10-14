// Снимки графики глазами: несколько моментов каждой графики в один ряд —
// середина появления, момент чтения (как у fitcheck), середина ухода.
//
//   node stills.mjs <samples.json> <папка> [фон.jpg|-] [мс,мс,…]
//
// samples: [[имя, шаблон, секунды, props], ...] — как у витрин в
// motion-showcase/. Фон (кадр камеры) — под графикой поверх лица; без него
// серый. Пишет <папка>/<имя>.jpg, 3 кадра 960×540 в ряд.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import puppeteer from 'puppeteer-core'
import { page as buildPage } from './templates.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const [samplesPath, outDir, bgPath, timesArg] = process.argv.slice(2)
// Свои моменты (мс через запятую) вместо «появление, чтение, уход» — смотреть движение
const fixedTimes = timesArg ? timesArg.split(',').map(Number) : null
if (!samplesPath || !outDir) {
  console.error('использование: node stills.mjs <samples.json> <папка> [фон.jpg]')
  process.exit(2)
}
const samples = JSON.parse(readFileSync(samplesPath, 'utf8'))
mkdirSync(outDir, { recursive: true })

function fontsCss() {
  const css = []
  const packs = [['golos-text', [400, 500, 600, 700]], ['jetbrains-mono', [400, 500]]]
  for (const [pkg, weights] of packs) {
    const dir = join(here, 'node_modules', '@fontsource', pkg)
    for (const w of weights) {
      css.push(readFileSync(join(dir, `${w}.css`), 'utf8').replace(/url\(\.\/files\//g, `url(${pathToFileURL(join(dir, 'files'))}/`))
    }
  }
  return css.join('\n')
}
const fonts = fontsCss()
const theme = readFileSync(join(here, 'theme.css'), 'utf8')
const bg = bgPath && bgPath !== '-' ? `html { background: #333 url("${pathToFileURL(resolve(bgPath)).href}") center / cover no-repeat; }` : 'html { background: #3a3a3a; }'
const chrome = process.env.CHROME ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-first-run', '--allow-file-access-from-files', '--hide-scrollbars'] })
try {
  const tab = await browser.newPage()
  await tab.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 0.5 })
  for (const [name, template, seconds, props] of samples) {
    const durationMs = seconds * 1000
    const file = join(outDir, `${name}.html`)
    const html = buildPage(template, props, { durationMs, fontsCss: fonts, themeCss: theme, base: `${pathToFileURL(here).href}/` })
      .replace('</style>', `${bg}</style>`)
    writeFileSync(file, html)
    await tab.goto(pathToFileURL(file).href, { waitUntil: 'load' })
    await tab.evaluate(() => document.fonts.ready)
    await tab.evaluate(() => Promise.all((window.__adapters ?? []).map((a) => a.ready)))
    // Моменты: как render.mjs и fitcheck — конец всех появлений и начало ухода
    const [from, to] = await tab.evaluate((D) => {
      let a = 0
      let b = D
      for (const x of document.getAnimations()) {
        const t = x.effect.getComputedTiming()
        if (/^(leave|fadeout)/.test(x.animationName ?? '')) b = Math.min(b, t.delay)
        else if (Number.isFinite(t.activeDuration)) a = Math.max(a, t.delay + t.activeDuration)
      }
      return [Math.min(a, D), b]
    }, durationMs)
    const read = Math.min(from + 1, to - 1)
    const times = fixedTimes ?? [Math.round(read * 0.35), Math.round(read), Math.round((to + durationMs) / 2)]
    const shots = []
    for (const t of times) {
      await tab.evaluate((ms) => window.__seek(ms), t)
      shots.push(await tab.screenshot({ type: 'jpeg', quality: 82, encoding: 'base64' }))
    }
    const row = `<html><body style="margin:0;display:flex;gap:6px;background:#000">${shots.map((s) => `<img src="data:image/jpeg;base64,${s}" width="960" height="540">`).join('')}</body></html>`
    await tab.setViewport({ width: 960 * shots.length + 6 * (shots.length - 1), height: 540, deviceScaleFactor: 1 })
    await tab.setContent(row, { waitUntil: 'load' })
    writeFileSync(join(outDir, `${name}.jpg`), await tab.screenshot({ type: 'jpeg', quality: 80 }))
    await tab.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 0.5 })
    console.log(`${name}: появление ${times[0]} мс, чтение ${times[1]} мс, уход ${times[2]} мс`)
  }
} finally {
  await browser.close()
}
