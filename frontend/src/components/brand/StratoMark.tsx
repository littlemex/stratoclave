/**
 * StratoMark — Stratoclave brand mark
 *
 * Concept:
 *   - Strato (stratification): three stacked diamond layers (top-to-bottom: "ice", "stone", "bedrock")
 *   - Conclave (assembly): a central light point that breathes continuously, with thin cross-rays converging from all four sides
 *
 * Props:
 *   - size: visual size in px
 *   - animated: true enables breathing + light-point animation; false is static (for use in Header, etc.)
 *   - parallax: [-1..1] range — the three layers tilt independently; used for mouse-tracking on the Login screen
 */

import { cn } from '@/lib/utils'

interface Props {
  size?: number
  animated?: boolean
  parallax?: { x: number; y: number }
  className?: string
}

export function StratoMark({
  size = 56,
  animated = false,
  parallax,
  className,
}: Props) {
  const px = parallax?.x ?? 0
  const py = parallax?.y ?? 0

  // Parallax amount per layer (upper layers move more = closer layers move faster)
  const layerStyle = (depth: number): React.CSSProperties => {
    const factor = depth // 0 (far back) .. 1 (front)
    return {
      transform: `translate3d(${px * factor * 6}px, ${py * factor * 6}px, 0)`,
      transition: 'transform 180ms cubic-bezier(0.22, 1, 0.36, 1)',
      transformOrigin: 'center',
    }
  }

  return (
    <div
      className={cn(
        'strato-mark relative grid place-items-center',
        animated && 'strato-mark--animated',
        className,
      )}
      style={{
        width: size,
        height: size,
        perspective: '400px',
      }}
      aria-hidden
    >
      {/* Background ice glow */}
      <div
        className="strato-mark__halo pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(circle at 50% 50%, hsl(200 85% 62% / 0.35), transparent 65%)',
        }}
      />

      <svg
        viewBox="0 0 120 120"
        width={size}
        height={size}
        className="relative"
        style={{
          transform: `rotateY(${px * 4}deg) rotateX(${-py * 4}deg)`,
          transformStyle: 'preserve-3d',
          transition: 'transform 180ms cubic-bezier(0.22, 1, 0.36, 1)',
        }}
      >
        <defs>
          {/* Ice layer - upper */}
          <linearGradient id="strato-ice" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(200 95% 78%)" stopOpacity="0.95" />
            <stop offset="100%" stopColor="hsl(200 85% 55%)" stopOpacity="0.85" />
          </linearGradient>
          {/* Stone layer - middle */}
          <linearGradient id="strato-stone" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(210 25% 70%)" stopOpacity="0.9" />
            <stop offset="100%" stopColor="hsl(215 20% 45%)" stopOpacity="0.7" />
          </linearGradient>
          {/* Bedrock - bottom layer */}
          <linearGradient id="strato-bedrock" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(220 15% 40%)" stopOpacity="0.6" />
            <stop offset="100%" stopColor="hsl(220 18% 20%)" stopOpacity="0.9" />
          </linearGradient>
          {/* Cardinal red bleed - bottom right */}
          <radialGradient id="strato-cardinal" cx="0.7" cy="0.7" r="0.5">
            <stop offset="0%" stopColor="hsl(355 70% 50%)" stopOpacity="0.35" />
            <stop offset="100%" stopColor="hsl(355 70% 50%)" stopOpacity="0" />
          </radialGradient>
          {/* Light ray */}
          <linearGradient id="strato-ray" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(200 95% 88%)" stopOpacity="0" />
            <stop offset="50%" stopColor="hsl(200 95% 88%)" stopOpacity="0.65" />
            <stop offset="100%" stopColor="hsl(200 95% 88%)" stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* Faint cardinal red bleed (furthest back) */}
        <rect x="0" y="0" width="120" height="120" fill="url(#strato-cardinal)" />

        {/* Bedrock (lowest layer, furthest back) */}
        <g style={layerStyle(0.2)}>
          <polygon
            points="60,98 34,72 60,46 86,72"
            fill="url(#strato-bedrock)"
            stroke="hsl(220 18% 28%)"
            strokeWidth="0.8"
          />
        </g>

        {/* Middle layer (stone) */}
        <g style={layerStyle(0.55)}>
          <polygon
            points="60,80 38,58 60,36 82,58"
            fill="url(#strato-stone)"
            stroke="hsl(215 20% 55%)"
            strokeWidth="0.8"
            strokeOpacity="0.6"
          />
        </g>

        {/* Upper layer (ice) */}
        <g style={layerStyle(1)}>
          <polygon
            points="60,62 44,46 60,30 76,46"
            fill="url(#strato-ice)"
            stroke="hsl(200 95% 85%)"
            strokeWidth="0.6"
            strokeOpacity="0.5"
          />
        </g>

        {/* Cross rays (Conclave convergence) */}
        <g className="strato-rays" style={layerStyle(0.8)}>
          <line
            x1="60"
            y1="6"
            x2="60"
            y2="22"
            stroke="url(#strato-ray)"
            strokeWidth="1"
          />
          <line
            x1="60"
            y1="98"
            x2="60"
            y2="114"
            stroke="url(#strato-ray)"
            strokeWidth="1"
          />
          <line
            x1="6"
            y1="60"
            x2="22"
            y2="60"
            stroke="url(#strato-ray)"
            strokeWidth="1"
            transform="rotate(90 60 60)"
          />
          <line
            x1="98"
            y1="60"
            x2="114"
            y2="60"
            stroke="url(#strato-ray)"
            strokeWidth="1"
            transform="rotate(90 60 60)"
          />
        </g>

        {/* Central assembly light point (Conclave focal point) */}
        <circle
          cx="60"
          cy="46"
          r="2.2"
          fill="hsl(200 95% 92%)"
          className="strato-core"
          style={{
            filter: 'drop-shadow(0 0 6px hsl(200 95% 85% / 0.9))',
          }}
        />
      </svg>
    </div>
  )
}
