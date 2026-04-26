import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'

import { AuthProvider } from '@/contexts/AuthContext'
import { ErrorProvider } from '@/contexts/ErrorContext'
import { loadRuntimeConfig } from '@/lib/runtimeConfig'
import { queryClient } from '@/lib/api'
import App from '@/App'
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
    if (container) {
      container.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:center;min-height:100vh;background:#0b0d12;color:#e7ecf3;font-family:Inter,system-ui,sans-serif;">
          <div style="max-width:420px;padding:2rem;border:1px solid #3f2b2b;background:#1a1216;">
            <h1 style="margin:0 0 12px 0;font-family:Fraunces,serif;">設定の読み込みに失敗しました</h1>
            <p style="margin:0;color:#b0a8a0;font-size:14px;">${error instanceof Error ? error.message : String(error)}</p>
          </div>
        </div>
      `
    }
  })
