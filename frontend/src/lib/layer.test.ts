import { describe, expect, it } from 'vitest'
import { graphicDetails, graphicTitle, shotDetails } from './layer'

describe('графика словами', () => {
  it('название', () => {
    expect(graphicTitle('steps', { steps: ['а', 'б', 'в'] })).toBe('Схема этапов (3)')
    expect(graphicTitle('command', { text: 'uv run longform-agent prj_1' })).toBe('Команда: uv run longform-agent prj_1')
    expect(graphicTitle('keyword', { text: 'Нужна проверка человеком' })).toBe('Акцент: Нужна проверка человеком')
  })
  it('вставка страницы и подпись', () => {
    expect(graphicTitle('page_insert', { page: '0123456789abcdef' })).toBe('Вставка страницы')
    expect(graphicDetails('lower_third', { title: 'Claude Code', subtitle: 'агент в терминале' }))
      .toEqual(['Claude Code', 'агент в терминале'])
  })
  it('движение кадра', () => {
    expect(shotDetails({ params: { punch: { scale: 1.12, cx: 0.5, cy: 0.42, release: 4 }, push: 0.04 } })).toEqual([
      'лицо крупнее ×1.12, за 4 с плавно назад к общему плану', 'медленный наезд на 4% на кусках от 5 с'])
    expect(shotDetails({ params: { drop: 0.08 } })).toEqual(['экран опущен на 8% высоты — сверху освобождается место'])
    expect(shotDetails({ params: { alternate: { step: 0.08, cx: 0.5, cy: 0.42 } } }))
      .toEqual(['план меняется на склейках: общий и крупнее на 8%'])
  })
  it('что на экране', () => {
    expect(graphicDetails('steps', { steps: ['читает', 'делает'], active: 1 })).toEqual(['1. читает — пройден', '2. делает — сейчас'])
    expect(graphicDetails('command', { text: 'ls -la', caption: 'в папке проекта' })).toEqual(['ls -la', 'в папке проекта'])
  })
})
