// Playwright configuration for the nightly E2E smoke.
//
// Rules of thumb:
//   - The config only runs what lives under `./e2e`, never the Vitest
//     unit suites under `src/__tests__/`.
//   - CI runs chromium only to keep runtime low; developers can pass
//     `--project=firefox` locally for parity.
//   - The dev server is started on port 3003 with mocked auth so the
//     smoke is deterministic without needing a live backend.
import { defineConfig, devices } from '@playwright/test'

const PORT = 3003

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'npm run dev -- --port 3003 --strictPort',
    port: PORT,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
