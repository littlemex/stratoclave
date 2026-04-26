import { Fragment } from 'react'
import { Link, NavLink, Outlet } from 'react-router-dom'
import { LogOut } from 'lucide-react'

import { StratoMark } from '@/components/brand/StratoMark'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/contexts/AuthContext'
import { usePermissions } from '@/hooks/usePermissions'
import { cn } from '@/lib/utils'

interface NavItem {
  to: string
  label: string
  when?: 'admin' | 'team_lead' | 'any'
}

const NAV: NavItem[] = [
  { to: '/', label: 'ダッシュボード' },
  { to: '/me/usage', label: '使用履歴' },
  { to: '/me/api-keys', label: 'API キー' },
  { to: '/admin/users', label: 'ユーザー管理', when: 'admin' },
  { to: '/admin/tenants', label: 'テナント管理', when: 'admin' },
  { to: '/admin/usage', label: '全体使用量', when: 'admin' },
  { to: '/admin/trusted-accounts', label: '信頼アカウント', when: 'admin' },
  { to: '/team-lead/tenants', label: '所有テナント', when: 'team_lead' },
]

export function AppShell() {
  const { state, logout } = useAuth()
  const { isAdmin, isTeamLead } = usePermissions()

  const items = NAV.filter((item) => {
    if (!item.when || item.when === 'any') return true
    if (item.when === 'admin') return isAdmin
    if (item.when === 'team_lead') return isTeamLead
    return false
  })

  return (
    <div className="min-h-screen text-foreground">
      <header className="sticky top-0 z-30 border-b border-border/60 bg-background/75 backdrop-blur-md supports-[backdrop-filter]:bg-background/60">
        <div className="relative mx-auto flex h-14 max-w-6xl items-center gap-4 px-6">
          <Link
            to="/"
            className="group flex items-center gap-2.5 font-display text-lg tracking-tight"
          >
            <StratoMark size={26} />
            <span className="transition-colors group-hover:text-primary">
              Stratoclave
            </span>
          </Link>
          <nav
            aria-label="主ナビゲーション"
            className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto"
          >
            {items.map((item, idx) => (
              <Fragment key={item.to}>
                {idx > 0 ? (
                  <span
                    aria-hidden
                    className="mx-1 h-3 w-px bg-border/60"
                  />
                ) : null}
                <NavLink
                  to={item.to}
                  end={item.to === '/'}
                  className={({ isActive }) =>
                    cn(
                      'relative whitespace-nowrap px-2.5 py-1.5 text-sm transition-colors',
                      'after:absolute after:left-2.5 after:right-2.5 after:-bottom-[13px] after:h-px after:transition-colors',
                      isActive
                        ? 'text-foreground after:bg-primary'
                        : 'text-muted-foreground hover:text-foreground after:bg-transparent',
                    )
                  }
                >
                  {item.label}
                </NavLink>
              </Fragment>
            ))}
          </nav>
          <div className="hidden min-w-0 max-w-[200px] truncate text-right text-xs text-muted-foreground md:block">
            {state.user?.email}
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={logout}
            aria-label="サインアウト"
            className="gap-2"
          >
            <LogOut className="h-4 w-4" />
            <span className="hidden md:inline">サインアウト</span>
          </Button>
        </div>
        <div aria-hidden className="strato-hairline" />
      </header>
      <main className="mx-auto w-full max-w-6xl px-6 py-10 md:py-12">
        <Outlet />
      </main>
      <footer className="mx-auto w-full max-w-6xl px-6 pb-10 pt-4 text-xs text-muted-foreground/70">
        <div className="strato-hairline strato-hairline--reverse mb-4" />
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span>Stratoclave · Bedrock proxy gateway</span>
          <span className="inline-flex items-center font-mono text-[11px] tracking-wide">
            <span className="strato-beacon" aria-hidden>
              <span className="strato-beacon__core" />
              <span className="strato-beacon__ring" />
              <span className="strato-beacon__ring strato-beacon__ring--delayed" />
            </span>
            tenant{' '}
            <span className="ml-1 text-muted-foreground">
              {state.user?.org_id}
            </span>
          </span>
        </div>
      </footer>
    </div>
  )
}
