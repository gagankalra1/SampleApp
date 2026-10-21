/**
 * Product imagery, generated rather than photographed.
 *
 * A sample app has no product photos, and placeholder grey boxes make a
 * storefront look broken. Instead each product gets a gradient keyed to its
 * own accent colour plus a line icon for its category. The result is
 * consistent, loads instantly, needs no external requests, and makes
 * categories recognisable at a glance in a grid.
 */

type Category =
  | 'laptops'
  | 'smartphones'
  | 'audio'
  | 'wearables'
  | 'televisions'
  | 'cameras'
  | 'gaming'
  | 'smart-home'
  | 'accessories'

const ICONS: Record<string, JSX.Element> = {
  laptops: (
    <>
      <rect x="7" y="10" width="34" height="22" rx="2.5" />
      <path d="M3 36h42l-3 4H6l-3-4z" />
    </>
  ),
  smartphones: (
    <>
      <rect x="16" y="6" width="16" height="36" rx="3" />
      <path d="M21 10h6" />
      <circle cx="24" cy="37" r="1.4" />
    </>
  ),
  audio: (
    <>
      <path d="M11 28v-5a13 13 0 0126 0v5" />
      <rect x="6" y="26" width="7" height="13" rx="3" />
      <rect x="35" y="26" width="7" height="13" rx="3" />
    </>
  ),
  wearables: (
    <>
      <rect x="14" y="13" width="20" height="22" rx="5" />
      <path d="M19 13V7h10v6M19 35v6h10v-6" />
      <path d="M24 20v5l3.5 2" />
    </>
  ),
  televisions: (
    <>
      <rect x="4" y="9" width="40" height="25" rx="2.5" />
      <path d="M17 40h14M24 34v6" />
    </>
  ),
  cameras: (
    <>
      <rect x="5" y="14" width="38" height="24" rx="3.5" />
      <path d="M17 14l3-4h8l3 4" />
      <circle cx="24" cy="26" r="7" />
      <circle cx="24" cy="26" r="2.5" />
    </>
  ),
  gaming: (
    <>
      <path d="M16 17h16a11 11 0 0110 11c0 4-2.5 6-5 6-3 0-4-2-7-2h-12c-3 0-4 2-7 2-2.5 0-5-2-5-6a11 11 0 0110-11z" />
      <path d="M15 25h5M17.5 22.5v5" />
      <circle cx="31" cy="24" r="1.6" />
      <circle cx="35" cy="28" r="1.6" />
    </>
  ),
  'smart-home': (
    <>
      <path d="M8 23l16-13 16 13" />
      <path d="M12 21v17h24V21" />
      <path d="M20 38V28h8v10" />
    </>
  ),
  accessories: (
    <>
      <path d="M24 6v14M24 28v14" />
      <rect x="15" y="20" width="18" height="8" rx="2.5" />
      <path d="M20 14h8" />
    </>
  ),
}

function hexToRgba(hex: string, alpha: number): string {
  const clean = hex.replace('#', '')
  const value = clean.length === 3 ? clean.split('').map((c) => c + c).join('') : clean
  const n = Number.parseInt(value, 16)
  if (Number.isNaN(n)) return `rgba(99, 102, 241, ${alpha})`
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

interface Props {
  category: string
  accent: string
  name: string
  className?: string
  /** Larger glyph and stronger wash, for the product detail page. */
  size?: 'tile' | 'hero'
}

export default function ProductVisual({ category, accent, name, className = '', size = 'tile' }: Props) {
  const icon = ICONS[category as Category] ?? ICONS.accessories
  const isHero = size === 'hero'

  return (
    <div
      className={`relative flex items-center justify-center overflow-hidden ${className}`}
      style={{
        background: `
          radial-gradient(120% 100% at 50% 0%, ${hexToRgba(accent, isHero ? 0.3 : 0.22)}, transparent 70%),
          linear-gradient(160deg, ${hexToRgba(accent, 0.14)}, rgba(11, 13, 23, 0.9) 65%)
        `,
      }}
    >
      {/* Faint grid, so the surface reads as a product shot backdrop rather
          than a flat swatch. */}
      <div
        aria-hidden="true"
        className="absolute inset-0 opacity-[0.07]"
        style={{
          backgroundImage:
            'linear-gradient(rgba(255,255,255,0.6) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.6) 1px, transparent 1px)',
          backgroundSize: '22px 22px',
        }}
      />
      <svg
        viewBox="0 0 48 48"
        className={`relative drop-shadow-lg ${isHero ? 'h-40 w-40' : 'h-20 w-20'}`}
        fill="none"
        stroke={accent}
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        role="img"
        aria-label={`${name} — illustrative ${category.replace('-', ' ')} graphic`}
      >
        {icon}
      </svg>
      <div
        aria-hidden="true"
        className="absolute -bottom-14 left-1/2 h-24 w-3/4 -translate-x-1/2 rounded-[50%] blur-2xl"
        style={{ background: hexToRgba(accent, 0.3) }}
      />
    </div>
  )
}
