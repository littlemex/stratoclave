/**
 * StratoMark — Stratoclave のブランドマーク
 *
 * コンセプト:
 *   - Strato (成層): 3 層の菱形 (上から「氷」「石」「岩盤」) を積み重ね
 *   - Conclave (集会): 中央の光点が常時呼吸し、四方から細い十字光が集束
 *
 * Props:
 *   - size: 視覚サイズ (px)
 *   - animated: true で breathing + 光点アニメーション、false は静止 (Header 等)
 *   - parallax: [-1..1] の範囲で 3 層が独立 tilt、Login 画面で mouse 追従
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

  // 層ごとの視差量 (上層ほど大きく動く = 近いほど速い)
  const layerStyle = (depth: number): React.CSSProperties => {
    const factor = depth // 0 (奥) .. 1 (手前)
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
      {/* 背景の氷光 */}
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
          {/* 氷層 - 上層 */}
          <linearGradient id="strato-ice" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(200 95% 78%)" stopOpacity="0.95" />
            <stop offset="100%" stopColor="hsl(200 85% 55%)" stopOpacity="0.85" />
          </linearGradient>
          {/* 石層 - 中層 */}
          <linearGradient id="strato-stone" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(210 25% 70%)" stopOpacity="0.9" />
            <stop offset="100%" stopColor="hsl(215 20% 45%)" stopOpacity="0.7" />
          </linearGradient>
          {/* 岩盤 - 底層 */}
          <linearGradient id="strato-bedrock" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(220 15% 40%)" stopOpacity="0.6" />
            <stop offset="100%" stopColor="hsl(220 18% 20%)" stopOpacity="0.9" />
          </linearGradient>
          {/* 枢機卿赤の滲み - 右下 */}
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

        {/* 枢機卿赤のわずかな滲み (最奥) */}
        <rect x="0" y="0" width="120" height="120" fill="url(#strato-cardinal)" />

        {/* 岩盤 (最下層、最奥) */}
        <g style={layerStyle(0.2)}>
          <polygon
            points="60,98 34,72 60,46 86,72"
            fill="url(#strato-bedrock)"
            stroke="hsl(220 18% 28%)"
            strokeWidth="0.8"
          />
        </g>

        {/* 中層 (石) */}
        <g style={layerStyle(0.55)}>
          <polygon
            points="60,80 38,58 60,36 82,58"
            fill="url(#strato-stone)"
            stroke="hsl(215 20% 55%)"
            strokeWidth="0.8"
            strokeOpacity="0.6"
          />
        </g>

        {/* 上層 (氷) */}
        <g style={layerStyle(1)}>
          <polygon
            points="60,62 44,46 60,30 76,46"
            fill="url(#strato-ice)"
            stroke="hsl(200 95% 85%)"
            strokeWidth="0.6"
            strokeOpacity="0.5"
          />
        </g>

        {/* 十字の光線 (Conclave の集束) */}
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

        {/* 中央の集会光点 (Conclave の焦点) */}
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
