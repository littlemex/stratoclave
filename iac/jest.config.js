module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.test.ts'],
  transform: {
    '^.+\\.tsx?$': 'ts-jest'
  },
  // TypeScript first, matching the `--prefer-ts-exts` that cdk.json already uses
  // to run the app. `npm run build` leaves compiled `lib/*.js` next to the
  // sources, and Jest's default order resolves those first — so a suite run after
  // a build tested the previous compile, and a source change with no rebuild
  // passed tests without ever being exercised.
  moduleFileExtensions: ['ts', 'tsx', 'js', 'mjs', 'cjs', 'jsx', 'json', 'node'],
  // Every worker type-checks the whole CDK app through ts-jest and settles around
  // 2GB resident. Jest defaults to cpuCount-1 workers, so a 10-core laptop starts
  // nine of them and asks for ~19GB, which exhausts memory and gets unrelated
  // processes killed. Cap it locally; CI runners get the wider pool.
  maxWorkers: process.env.CI ? '50%' : 2
};
