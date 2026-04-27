/* eslint-env node */
// Frontend ESLint config (eslint 8.x classic).
// v0.1.0 conservative defaults: TypeScript recommended + React hooks + Prettier.
module.exports = {
  root: true,
  env: { browser: true, es2022: true },
  extends: [
    "eslint:recommended",
    "plugin:@typescript-eslint/recommended",
    "plugin:react-hooks/recommended",
    "prettier", // must be last to disable stylistic rules that conflict with Prettier
  ],
  ignorePatterns: [
    "dist",
    "node_modules",
    "public",
    ".eslintrc.cjs",
    "vite.config.ts",
    "postcss.config.js",
    "tailwind.config.ts",
  ],
  parser: "@typescript-eslint/parser",
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: "module",
    project: false,
  },
  plugins: ["react-refresh"],
  rules: {
    "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
    "@typescript-eslint/no-unused-vars": [
      "warn",
      { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
    ],
    "@typescript-eslint/no-explicit-any": "off", // tolerated for v0.1.0; tighten later
    "no-console": ["warn", { allow: ["warn", "error"] }],
  },
};
