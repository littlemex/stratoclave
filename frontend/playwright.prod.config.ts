// Playwright config for the live-deployment E2E suite. Unlike
// `playwright.config.ts`, this variant does NOT spin up the local
// Vite dev server — every assertion targets the deployed backend
// reached via `PROD_FRONTEND_URL`.
import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.PROD_FRONTEND_URL ?? 'https://example.invalid'

export default defineConfig({
  testDir: './e2e',
  testMatch: [
    'prod-deploy-2026-06-11.spec.ts',
    'prod-authenticated-shell.spec.ts',
    'prod-p0-ui.spec.ts',
    'prod-billing-redaction.spec.ts',
    'prod-authcap-badge.spec.ts',
  ],
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: 'list',
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
})
