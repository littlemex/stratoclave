import { useEffect, useRef, useState } from 'react'
import { Loader2, Terminal } from 'lucide-react'
import { Trans, useTranslation } from 'react-i18next'

import { StratoMark } from '@/components/brand/StratoMark'
import { LanguageSwitcher } from '@/components/common/LanguageSwitcher'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { useAuth } from '@/contexts/AuthContext'

/**
 * Login screen
 * - Background, logo, card, and cursor halo all react to mouse position
 * - Mouse coordinates are injected directly into CSS custom properties (--mx/--my/--sx/--sy/--cx/--cy) via rAF + spring interpolation
 * - The card tilts within ±3 degrees via rotateX/Y (a peering-in perspective)
 * - Respects prefers-reduced-motion by disabling all motion
 */

/** The "no tokens" state on first visit is normal and should not be displayed as an error. */
const BENIGN_AUTH_MESSAGES = new Set([
  'No tokens',
  'No tokens found',
  'No refresh token',
  'Session expired',
  'Session invalid',
])

function isBenignAuthState(message: string | null | undefined): boolean {
  if (!message) return true
  return BENIGN_AUTH_MESSAGES.has(message)
}
export default function Login() {
  const { login, state } = useAuth()
  const { t } = useTranslation()
  const [loading, setLoading] = useState(false)

  const containerRef = useRef<HTMLDivElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const rafRef = useRef<number | null>(null)
  // targetRef: mouse position in [-1..1], currentRef: spring-interpolated current value
  const targetRef = useRef({ x: 0, y: 0, cx: 0, cy: 0 })
  const currentRef = useRef({ x: 0, y: 0, cx: 0, cy: 0 })
  const [parallax, setParallax] = useState({ x: 0, y: 0 })

  useEffect(() => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduce) return

    const onMove = (e: MouseEvent) => {
      const container = containerRef.current
      if (!container) return
      const rect = container.getBoundingClientRect()
      const x = ((e.clientX - rect.left) / rect.width) * 2 - 1
      const y = ((e.clientY - rect.top) / rect.height) * 2 - 1
      targetRef.current = {
        x: Math.max(-1, Math.min(1, x)),
        y: Math.max(-1, Math.min(1, y)),
        cx: e.clientX,
        cy: e.clientY,
      }
    }
    const onLeave = () => {
      targetRef.current = {
        x: 0,
        y: 0,
        cx: targetRef.current.cx,
        cy: targetRef.current.cy,
      }
    }

    const tick = () => {
      // spring interpolation
      const dx = targetRef.current.x - currentRef.current.x
      const dy = targetRef.current.y - currentRef.current.y
      const dcx = targetRef.current.cx - currentRef.current.cx
      const dcy = targetRef.current.cy - currentRef.current.cy

      const needsUpdate =
        Math.abs(dx) > 0.001 ||
        Math.abs(dy) > 0.001 ||
        Math.abs(dcx) > 0.5 ||
        Math.abs(dcy) > 0.5

      if (needsUpdate) {
        currentRef.current = {
          x: currentRef.current.x + dx * 0.08,
          y: currentRef.current.y + dy * 0.08,
          cx: currentRef.current.cx + dcx * 0.18,
          cy: currentRef.current.cy + dcy * 0.18,
        }

        const container = containerRef.current
        if (container) {
          // Position within the page as 0..100% (for spotlight / aurora)
          const rect = container.getBoundingClientRect()
          const sx = Math.max(
            0,
            Math.min(100, ((currentRef.current.cx - rect.left) / rect.width) * 100),
          )
          const sy = Math.max(
            0,
            Math.min(100, ((currentRef.current.cy - rect.top) / rect.height) * 100),
          )
          container.style.setProperty('--mx', currentRef.current.x.toFixed(3))
          container.style.setProperty('--my', currentRef.current.y.toFixed(3))
          container.style.setProperty('--sx', sx.toFixed(2))
          container.style.setProperty('--sy', sy.toFixed(2))
          container.style.setProperty('--cx', `${currentRef.current.cx.toFixed(1)}px`)
          container.style.setProperty('--cy', `${currentRef.current.cy.toFixed(1)}px`)
        }

        // Card tilt (subtle: maximum ±3 degrees)
        if (cardRef.current) {
          const tiltX = (-currentRef.current.y * 3).toFixed(2)
          const tiltY = (currentRef.current.x * 3).toFixed(2)
          cardRef.current.style.transform = `perspective(1200px) rotateX(${tiltX}deg) rotateY(${tiltY}deg)`
        }

        setParallax({ x: currentRef.current.x, y: currentRef.current.y })
      }
      rafRef.current = requestAnimationFrame(tick)
    }

    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseleave', onLeave)
    rafRef.current = requestAnimationFrame(tick)

    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseleave', onLeave)
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    }
  }, [])

  const handleLogin = async () => {
    setLoading(true)
    try {
      await login()
    } catch (err) {
      console.error('[Login] failed to start OAuth flow', err)
      setLoading(false)
    }
  }

  return (
    <div
      ref={containerRef}
      className="strato-login relative flex min-h-screen items-center justify-center overflow-hidden px-6 py-16"
      style={
        {
          '--mx': 0,
          '--my': 0,
          '--sx': 50,
          '--sy': 50,
          '--cx': '0px',
          '--cy': '0px',
        } as React.CSSProperties
      }
    >
      <BackgroundStrata />
      <div className="strato-cursor-halo" aria-hidden />

      <div className="relative z-10 w-full max-w-md">
        <div className="mb-10 flex flex-col items-center text-center">
          <StratoMark size={84} animated parallax={parallax} className="mb-5" />
          <h1 className="strato-title-shimmer font-display text-[44px] font-semibold leading-none tracking-tight">
            {t('login.title')}
          </h1>
          <p className="mt-3 max-w-xs text-sm text-muted-foreground">
            {t('app.tagline')}
          </p>
          <div className="mt-4">
            <LanguageSwitcher />
          </div>
        </div>

        <div ref={cardRef} className="strato-card-tilt">
          <Card variant="soft" className="strato-glass strato-card-spotlight">
            <CardHeader>
              <CardTitle className="text-2xl">{t('login.card_title')}</CardTitle>
              <CardDescription>
                {t('login.card_description')}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <Button
                size="lg"
                className="w-full"
                onClick={handleLogin}
                disabled={loading}
              >
                {loading ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                ) : null}
                {t('login.cta')}
              </Button>

              {state.error && !isBenignAuthState(state.error) ? (
                <p
                  role="alert"
                  className="border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive-foreground"
                >
                  {state.error}
                </p>
              ) : null}

              <div className="space-y-3 border-t border-border/60 pt-5">
                <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
                  <Terminal className="h-3.5 w-3.5" aria-hidden />
                  {t('login.cli_title')}
                </div>
                <pre className="overflow-x-auto border border-border/60 bg-muted/60 px-3 py-2 font-mono text-xs leading-relaxed text-muted-foreground">
{`stratoclave auth login
stratoclave ui open`}
                </pre>
                <p className="text-xs text-muted-foreground">
                  <Trans
                    i18nKey="login.cli_footer"
                    values={{ cmd: 'stratoclave ui open' }}
                    components={{
                      1: <code className="font-mono text-foreground/80" />,
                    }}
                  />
                </p>
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="mt-10 space-y-1.5 text-center">
          <p className="font-mono text-[11px] tracking-[0.16em] text-muted-foreground/80">
            {t('login.tagline_strato')}
          </p>
          <p className="font-mono text-[11px] tracking-[0.16em] text-muted-foreground/80">
            {t('login.tagline_conclave')}
          </p>
        </div>
      </div>
    </div>
  )
}

/**
 * Background decoration:
 *  - Glacier blob (primary) : drawn toward the mouse
 *  - Cardinal red blob       : moves in the opposite direction
 *  - 3 stratum lines         : tilt subtly with mouse X
 *  - 3 aurora threads        : full-width light lines that undulate with mouse Y
 *  - Particles               : static, glacial powder-snow feel
 */
function BackgroundStrata() {
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-0 -z-10 overflow-hidden"
    >
      {/* Glacial glow: drawn toward the mouse */}
      <div
        className="absolute left-1/2 top-[-5%] h-[70vh] w-[95vw] -translate-x-1/2 blur-3xl"
        style={{
          background:
            'radial-gradient(ellipse at center, hsl(200 80% 45% / 0.26), transparent 60%)',
          transform:
            'translate3d(calc(var(--mx, 0) * 40px - 50%), calc(var(--my, 0) * 30px), 0)',
          transition: 'transform 220ms cubic-bezier(0.22, 1, 0.36, 1)',
        }}
      />

      {/* Cardinal red: moves in the opposite direction */}
      <div
        className="absolute bottom-[-15%] left-[-10%] h-[46vh] w-[48vw] blur-3xl"
        style={{
          background:
            'radial-gradient(ellipse at center, hsl(355 60% 48% / 0.18), transparent 65%)',
          transform:
            'translate3d(calc(var(--mx, 0) * -30px), calc(var(--my, 0) * -22px), 0)',
          transition: 'transform 260ms cubic-bezier(0.22, 1, 0.36, 1)',
        }}
      />

      {/* 3 aurora threads (undulate with mouse Y) */}
      <div className="strato-aurora strato-aurora--a" />
      <div className="strato-aurora strato-aurora--b" />
      <div className="strato-aurora strato-aurora--c" />

      {/* Stratum lines (tilt with mouse X) */}
      <div
        className="absolute inset-x-0 top-[42%] h-px"
        style={{
          background:
            'linear-gradient(90deg, transparent, hsl(220 12% 30%) 20%, hsl(220 12% 30%) 80%, transparent)',
          transform: 'rotate(calc(var(--mx, 0) * 0.4deg))',
          transformOrigin: 'center',
          transition: 'transform 280ms cubic-bezier(0.22, 1, 0.36, 1)',
        }}
      />
      <div
        className="absolute inset-x-0 top-[60%] h-px"
        style={{
          background:
            'linear-gradient(90deg, transparent, hsl(220 12% 24%) 30%, hsl(220 12% 24%) 70%, transparent)',
          transform: 'rotate(calc(var(--mx, 0) * 0.25deg))',
          transformOrigin: 'center',
          transition: 'transform 280ms cubic-bezier(0.22, 1, 0.36, 1)',
        }}
      />
      <div
        className="absolute inset-x-0 top-[76%] h-px"
        style={{
          background:
            'linear-gradient(90deg, transparent, hsl(220 12% 20%) 35%, hsl(220 12% 20%) 65%, transparent)',
          transform: 'rotate(calc(var(--mx, 0) * 0.15deg))',
          transformOrigin: 'center',
          transition: 'transform 280ms cubic-bezier(0.22, 1, 0.36, 1)',
        }}
      />

      {/* Particles (static, glacial powder-snow feel) */}
      <svg
        className="absolute inset-0 opacity-[0.18]"
        width="100%"
        height="100%"
        aria-hidden
      >
        <defs>
          <pattern
            id="strato-grain"
            width="3"
            height="3"
            patternUnits="userSpaceOnUse"
          >
            <circle cx="1" cy="1" r="0.3" fill="hsl(210 25% 70%)" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#strato-grain)" />
      </svg>
    </div>
  )
}
