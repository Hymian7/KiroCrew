/**
 * haptic — one best-effort tap per platform.
 *
 *  - Android (navigator.vibrate present): each kind maps to its own pattern.
 *  - iOS (no vibrate API, iPhone UA): a hidden `<input type=checkbox switch>`
 *    is clicked once per call and created only once per document.
 *  - desktop / jsdom (neither): a silent no-op that touches nothing.
 *  - a throwing engine never propagates: feedback must not break the action.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

async function freshHaptic() {
  vi.resetModules()
  return (await import('./haptic')).haptic
}

describe('haptic', () => {
  const originalUA = navigator.userAgent

  beforeEach(() => {
    delete (navigator as unknown as { vibrate?: unknown }).vibrate
  })

  afterEach(() => {
    Object.defineProperty(navigator, 'userAgent', { value: originalUA, configurable: true })
    delete (navigator as unknown as { vibrate?: unknown }).vibrate
    document.body.innerHTML = ''
  })

  it('vibrates with a per-kind pattern where the API exists', async () => {
    const vibrate = vi.fn(() => true)
    Object.defineProperty(navigator, 'vibrate', { value: vibrate, configurable: true, writable: true })
    const haptic = await freshHaptic()
    haptic()
    haptic('medium')
    haptic('success')
    haptic('error')
    expect(vibrate.mock.calls.map(c => c[0])).toEqual([10, 20, [10, 40, 10], [30, 40, 30]])
  })

  it('is a silent no-op on a device with no engine', async () => {
    const haptic = await freshHaptic()
    expect(() => haptic('error')).not.toThrow()
    expect(document.body.querySelector('input[switch]')).toBeNull()
  })

  it('clicks one hidden switch on iOS and reuses it', async () => {
    Object.defineProperty(navigator, 'userAgent', {
      value: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15',
      configurable: true,
    })
    const haptic = await freshHaptic()
    haptic('light')
    haptic('success')
    const switches = document.body.querySelectorAll('input[type=checkbox][switch]')
    expect(switches).toHaveLength(1)
    expect(switches[0].closest('label')?.getAttribute('aria-hidden')).toBe('true')
  })

  it('swallows a throwing engine', async () => {
    Object.defineProperty(navigator, 'vibrate', {
      value: () => { throw new Error('no motor') },
      configurable: true,
      writable: true,
    })
    const haptic = await freshHaptic()
    expect(() => haptic('medium')).not.toThrow()
  })
})
