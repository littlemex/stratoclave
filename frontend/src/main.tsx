import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'

import { AuthProvider } from '@/contexts/AuthContext'
import { ErrorProvider } from '@/contexts/ErrorContext'
import { loadRuntimeConfig } from '@/lib/runtimeConfig'
import { queryClient } from '@/lib/api'
import App from '@/App'
// i18n side-effect: sets up `i18n.t` / `useTranslation()` before any
// component renders. Do NOT move this below component imports — React
// components that call `useTranslation()` during module init would
// otherwise run against an uninitialised instance.
import '@/lib/i18n'
import '@/index.css'

// 開発モードでのみ axe-core を起動。
// Build 時は vite が dev ブランチを tree-shake で除去する。
if (import.meta.env.DEV) {
  void (async () => {
    const axe = (await import('@axe-core/react')).default
    axe(React, ReactDOM, 1000, {
      rules: [
        { id: 'color-contrast', enabled: true },
        { id: 'aria-allowed-attr', enabled: true },
      ],
    })
  })()
}

loadRuntimeConfig()
  .then(() => {
    ReactDOM.createRoot(document.getElementById('root')!).render(
      <React.StrictMode>
        <BrowserRouter>
          <QueryClientProvider client={queryClient}>
            <ErrorProvider>
              <AuthProvider>
                <App />
              </AuthProvider>
            </ErrorProvider>
          </QueryClientProvider>
        </BrowserRouter>
      </React.StrictMode>,
    )
  })
  .catch((error) => {
    const container = document.getElementById('root')
    if (!container) return
    // P0-11 (sweep-4): the error message comes from a fetch surface we
    // do not fully control (proxies, DNS errors, custom /config.json).
    // We must treat it as untrusted text and render it through
    // `textContent`, never `innerHTML` / template-literal interpolation.
    // Runtime config failure happens before i18n can be trusted so we
    // keep the splash bilingual via static strings.
    while (container.firstChild) container.removeChild(container.firstChild)

    const outer = document.createElement('div')
    outer.setAttribute(
      'style',
      'display:flex;align-items:center;justify-content:center;min-height:100vh;background:#0b0d12;color:#e7ecf3;font-family:Inter,system-ui,sans-serif;',
    )
    const card = document.createElement('div')
    card.setAttribute(
      'style',
      'max-width:420px;padding:2rem;border:1px solid #3f2b2b;background:#1a1216;',
    )
    const heading = document.createElement('h1')
    heading.setAttribute('style', 'margin:0 0 12px 0;font-family:Fraunces,serif;')
    heading.textContent =
      'Configuration load failed / 設定の読み込みに失敗しました'
    const body = document.createElement('p')
    body.setAttribute('style', 'margin:0;color:#b0a8a0;font-size:14px;')
    body.textContent = error instanceof Error ? error.message : String(error)

    card.appendChild(heading)
    card.appendChild(body)
    outer.appendChild(card)
    container.appendChild(outer)
  })
