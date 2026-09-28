// One haptic tap, best effort. Android: `navigator.vibrate`. iOS 18+ Safari and
// PWAs expose no vibration API, but clicking a hidden `<input type=checkbox switch>`
// fires the Taptic engine -- the same trick every haptic npm package wraps.
// Everywhere else (desktop, Windows, jsdom) this is a silent no-op, and it never
// throws: feedback must not break the action it decorates.
export type HapticKind = 'light' | 'medium' | 'success' | 'error'

const VIBRATE_MS: Record<HapticKind, number | number[]> = {
  light: 10,
  medium: 20,
  success: [10, 40, 10],
  error: [30, 40, 30],
}

let iosSwitch: HTMLLabelElement | null = null

function isIOS(): boolean {
  return /iPhone|iPad|iPod/.test(navigator.userAgent) && !('vibrate' in navigator)
}

function iosTap(): void {
  if (!iosSwitch) {
    iosSwitch = document.createElement('label')
    iosSwitch.setAttribute('aria-hidden', 'true')
    iosSwitch.style.cssText = 'position:fixed;width:0;height:0;overflow:hidden;opacity:0;pointer-events:none'
    const input = document.createElement('input')
    input.type = 'checkbox'
    input.setAttribute('switch', '')
    iosSwitch.appendChild(input)
    document.body.appendChild(iosSwitch)
  }
  iosSwitch.click()
}

export function haptic(kind: HapticKind = 'light'): void {
  if (typeof navigator === 'undefined') return
  try {
    if ('vibrate' in navigator) navigator.vibrate(VIBRATE_MS[kind])
    else if (isIOS()) iosTap()
  } catch {
    // no haptics on this device
  }
}
