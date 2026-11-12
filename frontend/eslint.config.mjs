// ESLint 10 flat config for the frontend (React + Vite) workspace.
//
// Migrated from the legacy `.eslintrc.cjs` (eslint 8.x classic config, removed
// in this change). Same rule set and severities as before: eslint:recommended +
// @typescript-eslint/recommended, react-hooks's two original rules, and
// react-refresh's Vite preset (fast-refresh-safe exports), with Prettier last
// so its config disables any stylistic rule that would conflict with the
// formatter.
//
// react-hooks 7.x also ships a much larger "recommended-latest" bundle (the
// React Compiler-era rules: static-components, immutability, purity,
// set-state-in-render, etc., all at "error"). That bundle is deliberately NOT
// adopted here: this repository was never linted against those rules, and
// turning them on as blocking errors in a dependency-bump change would make
// this PR responsible for findings across the whole component tree that have
// nothing to do with any package version. Same v0.1.0 "conservative scope"
// posture the backend's ruff config documents for its own rule selection.
// A follow-up PR can audit and adopt the fuller bundle deliberately.
import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import prettier from 'eslint-config-prettier'

export default [
  {
    ignores: ['dist/**', 'node_modules/**', 'public/**', 'playwright-report/**', 'test-results/**'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        // Browser globals the classic config's `env: { browser: true }` provided.
        window: 'readonly',
        document: 'readonly',
        navigator: 'readonly',
        console: 'readonly',
        fetch: 'readonly',
        localStorage: 'readonly',
        sessionStorage: 'readonly',
        URL: 'readonly',
        URLSearchParams: 'readonly',
        AbortController: 'readonly',
        setTimeout: 'readonly',
        clearTimeout: 'readonly',
        setInterval: 'readonly',
        clearInterval: 'readonly',
        requestAnimationFrame: 'readonly',
        cancelAnimationFrame: 'readonly',
        performance: 'readonly',
        CustomEvent: 'readonly',
        HTMLElement: 'readonly',
        MouseEvent: 'readonly',
        // Node/Vite globals used by config and test files.
        process: 'readonly',
        __dirname: 'readonly',
        global: 'readonly',
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      // react-hooks/recommended (v4 line, as the pre-migration config used):
      // only the two original hook-correctness rules, not v7's much larger
      // React Compiler rule set (see file header).
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      '@typescript-eslint/no-explicit-any': 'off', // tolerated for v0.1.0; tighten later
      'no-console': ['warn', { allow: ['warn', 'error'] }],
    },
  },
  {
    // Playwright fixtures under e2e/support/ follow Playwright's own
    // `{ page }, use) => { ...; await use(page); ... }` fixture shape, not a
    // React component or hook. react-hooks 7's rules-of-hooks heuristically
    // flags any call to a function whose argument is named `use` as a hook
    // call outside a component/hook -- a false positive here, since nothing
    // under e2e/ renders React.
    files: ['e2e/**/*.{ts,tsx}'],
    rules: {
      'react-hooks/rules-of-hooks': 'off',
      'react-hooks/exhaustive-deps': 'off',
      'react-refresh/only-export-components': 'off',
    },
  },
  prettier,
]
