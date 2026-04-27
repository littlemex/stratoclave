/* eslint-env node */
// IaC (AWS CDK) ESLint config (eslint 8.x classic).
// Conservative defaults — v0.1.0 allows `any` and loose types to keep diffs
// small. Tighten in a follow-up PR.
module.exports = {
  root: true,
  env: { node: true, es2022: true },
  extends: ["eslint:recommended", "plugin:@typescript-eslint/recommended", "prettier"],
  ignorePatterns: [
    "node_modules",
    "cdk.out",
    "dist",
    "*.js",
    "*.d.ts",
    "!.eslintrc.cjs",
    "lib/cloudfront-functions/*.js",
  ],
  parser: "@typescript-eslint/parser",
  parserOptions: { ecmaVersion: 2022, sourceType: "module" },
  rules: {
    "@typescript-eslint/no-unused-vars": [
      "warn",
      { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
    ],
    "@typescript-eslint/no-explicit-any": "off",
    "@typescript-eslint/no-non-null-assertion": "off",
  },
};
