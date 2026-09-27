/**
 * The composer sits inside ONE Liquid Glass dock pane (`composer-dock`, built
 * from components/Glass.tsx): `--glass-tint` over the blurred transcript, the
 * `--glass-edge` hairline on the pane's outer box, and the composer halo for
 * depth. The pane also holds an approval bar fused to the composer's top and the
 * collapsed bar, so those share the material instead of meeting it at a seam;
 * the wrapper's own surface and border are therefore transparent in every mode
 * (an incognito / temporary session still paints its coloured border). The pane
 * is always mounted: toggling it would remount the editor and drop the draft's
 * focus when an approval lands.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))
import { screen } from '@testing-library/react'
import ChatInput from '../components/ChatInput'
import { createTestStore, renderWithProviders } from './helpers'
import type { RootState } from '../store'

const INDEX_CSS = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')

const dockOf = (wrapper: HTMLElement) => wrapper.closest('[data-testid="composer-dock"]') as HTMLElement

describe('composer liquid glass', () => {
  it('keeps the wrapper transparent so the dock pane shows through', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const wrapper = screen.getByTestId('input-wrapper')
    expect(wrapper.className).toContain('bg-transparent')
    expect(wrapper.className).toContain('border-transparent')
    expect(wrapper.className).not.toContain('bg-bg-elevated')
  })

  // With an approval box fused above, the bar and the composer share the ONE
  // dock pane: the wrapper stays transparent (no seam, no notch), keeps its
  // focus-within accent brightening, and the dock swaps its halo for the
  // approval glow so the pending decision is what lights up.
  it('keeps the wrapper on the shared pane and lights the approval glow while an approval is attached', () => {
    const store = createTestStore({
      chat: {
        activeSlot: 'slot-1',
        messages: [
          { role: 'user', content: 'list files' },
          {
            role: 'permission',
            content: 'Running: ls /tmp',
            meta: { approval_id: 'ap-1', request_id: 'req-1', tool_input: '{"command":"ls /tmp"}', tool_title: 'Running: ls /tmp', tool_call_id: 'tc-1' },
          },
        ],
        toolLog: [],
        slotStatusDetail: {},
      } as unknown as RootState['chat'],
      dashboard: {
        slots: [{ key: 'slot-1', messages: 2, running: true, pending_approval: true, waiting_for_input: false }],
        approvalMode: 'normal',
        connected: true,
        channelTrusted: false,
        refreshTrigger: 0,
        unreadSlots: [],
        updateProgress: null,
      } as unknown as RootState['dashboard'],
    })
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />, { store })
    const wrapper = screen.getByTestId('input-wrapper')
    expect(wrapper.className).toContain('bg-transparent')
    expect(wrapper.className).toContain('focus-within:border-accent/50')
    expect(wrapper.className).not.toContain('bg-bg-elevated')
    const dock = dockOf(wrapper)
    expect(dock.className).toContain('approval-glow')
    expect(dock.className).not.toContain('composer-halo')
    expect(screen.getByRole('button', { name: /allow once/i })).toBeTruthy()
  })

  it('mounts one dock pane around the wrapper: hairline box, halo, 16px glass, theme tint', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const wrapper = screen.getByTestId('input-wrapper')
    const dock = dockOf(wrapper)
    expect(dock).not.toBeNull()
    expect(dock.className).toContain('border-[color:var(--glass-edge)]')
    expect(dock.className).toContain('composer-halo')
    expect(dock.style.borderRadius).toBe('17px')
    // Glass renders [outer box > LiquidGlass root > content div > children]; the
    // root carries the radius and the tint layer sits among its effect layers.
    const root = dock.firstElementChild as HTMLElement
    expect(root.classList.contains('liquid-glass')).toBe(true)
    expect(root.style.borderRadius).toBe('16px')
    // The tint rides the oversized frost box inside the clipping effect layer.
    const boxes = Array.from(root.querySelectorAll<HTMLElement>('div[aria-hidden="true"] > div'))
    expect(boxes.some(l => l.style.background.includes('var(--glass-tint)'))).toBe(true)
  })

  it('defines --glass-tint and --glass-edge for both polarities', () => {
    expect(INDEX_CSS).toMatch(/:root \{ --glass-tint: rgba\(30, 30, 34, 0\.40\); --glass-edge: rgba\(255, 255, 255, 0\.14\); \}/)
    expect(INDEX_CSS).toMatch(/\[data-mode="light"\] \{ --glass-tint: rgba\(238, 238, 243, 0\.45\); --glass-edge: rgba\(0, 0, 0, 0\.16\); \}/)
  })

  // Both forms of the material must solidify wherever the app's other glass
  // does: reduced transparency, increased contrast, and a Chromium built without
  // backdrop-filter (#1817) — otherwise the transcript would show through the
  // box the user is typing into and through every chip above it.
  it('solidifies the pane and the CSS panes under every glass fallback rule', () => {
    for (const block of [/@supports not \(\(backdrop-filter[\s\S]*?\n\}/, /@media \(prefers-reduced-transparency: reduce\)\{[\s\S]*?\n\}/, /@media \(prefers-contrast: more\)\{[\s\S]*?\n\}/]) {
      const rule = INDEX_CSS.match(block)?.[0] ?? ''
      expect(rule, String(block)).toContain('.liquid-glass,.glass-pane{ background:var(--bg-elevated) !important')
      expect(rule, String(block)).toContain('.liquid-glass>[aria-hidden="true"]{ display:none !important }')
    }
  })
})
