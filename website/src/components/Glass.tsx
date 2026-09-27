/**
 * Glass — the one place the Liquid Glass recipe lives.
 *
 * Every surface that floats over the transcript in the composer dock (the
 * composer itself, an approval bar, the follow-up chips, a tip or suggestion
 * card, the queue, the memory chip, the jump-to-bottom button) and the mobile
 * Settings search capsule wear the SAME material: `--glass-tint` over a
 * blurred backdrop, an even top/bottom light band and a `--glass-edge`
 * hairline. Call sites say what they are (`variant`) and how round they are
 * (`radius`); they never restate the optics.
 *
 * Two variants, one recipe: `panel` (frost 12, light 24) for the composer-sized
 * boxes and `chip` (frost 8, light 18) for the small pills and cards, where
 * the panel numbers read heavy at 30px tall.
 *
 * Structure is [outer box → LiquidGlass → children]. The hairline lives on the
 * outer box: a lit white rim vanishes on a white page, so the edge is a
 * polarity-fixed hairline (the primitive draws no rim of its own). The bend,
 * its depth, the light direction and the fill token are constants inside the
 * primitive. `className` goes on the outer box and carries LAYOUT (margin,
 * width, flex) plus the box-shadow state the caller owns — `composer-halo`
 * with its focus-within glow, or `approval-glow` while a decision is pending —
 * because which shadow a pane wears at this instant is the caller's state, not
 * the material's. The optics themselves are not open for override here —
 * change the recipe, not the call site.
 */
import React from 'react'
import { LiquidGlass, type LiquidGlassProps } from './ui/liquid-glass'

export type GlassVariant = 'panel' | 'chip'

const RECIPE: Record<GlassVariant, Pick<LiquidGlassProps, 'frost' | 'lightIntensity'>> = {
  panel: { frost: 12, lightIntensity: 24 },
  chip: { frost: 8, lightIntensity: 18 },
}

export interface GlassProps extends Omit<React.HTMLAttributes<HTMLDivElement>, 'children'> {
  variant?: GlassVariant
  /** Corner radius in px of the pane; the outer hairline box takes radius + 1. */
  radius: number
  children?: React.ReactNode
}

export function Glass({
  variant = 'panel',
  radius,
  className,
  style,
  children,
  ...rest
}: GlassProps) {
  return (
    <div
      {...rest}
      className={['border border-[color:var(--glass-edge)]', className ?? ''].filter(Boolean).join(' ')}
      style={{ borderRadius: radius + 1, ...style }}
    >
      <LiquidGlass {...RECIPE[variant]} cornerRadius={radius}>
        {children}
      </LiquidGlass>
    </div>
  )
}

export default Glass
