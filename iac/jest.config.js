module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.test.ts'],
  transform: {
    '^.+\\.tsx?$': 'ts-jest'
  },
  // Every worker type-checks the whole CDK app through ts-jest and settles around
  // 2GB resident. Jest defaults to cpuCount-1 workers, so a 10-core laptop starts
  // nine of them and asks for ~19GB, which exhausts memory and gets unrelated
  // processes killed. Cap it locally; CI runners get the wider pool.
  maxWorkers: process.env.CI ? '50%' : 2
};
