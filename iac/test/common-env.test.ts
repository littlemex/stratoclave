import { optionalPositiveIntFromEnv, positiveIntFromEnv } from '../lib/_common';

describe('positiveIntFromEnv', () => {
  const NAME = 'TEST_SIZING_KNOB';

  afterEach(() => {
    delete process.env[NAME];
  });

  test('returns the fallback when unset or empty', () => {
    expect(positiveIntFromEnv(NAME, 128)).toBe(128);
    process.env[NAME] = '';
    expect(positiveIntFromEnv(NAME, 128)).toBe(128);
  });

  test('returns the configured value', () => {
    process.env[NAME] = '512';
    expect(positiveIntFromEnv(NAME, 128)).toBe(512);
  });

  test.each(['0', '-1', 'many', '1.5', '12x', '1e3', '0x80', ' 128 ', '+128', '128\n'])(
    'rejects %s at synth time rather than sizing a fleet from it',
    (bad) => {
      // Falling back silently would hide a deploy typo behind a fleet that is
      // quietly the wrong size; NaN would reach CloudFormation.
      process.env[NAME] = bad;
      expect(() => positiveIntFromEnv(NAME, 128)).toThrow(NAME);
    },
  );
});

describe('optionalPositiveIntFromEnv', () => {
  const NAME = 'TEST_MEASURED_KNOB';

  afterEach(() => {
    delete process.env[NAME];
  });

  test('is undefined when unset, so a consumer can leave the policy off', () => {
    expect(optionalPositiveIntFromEnv(NAME)).toBeUndefined();
    process.env[NAME] = '';
    expect(optionalPositiveIntFromEnv(NAME)).toBeUndefined();
  });

  test('returns the configured value', () => {
    process.env[NAME] = '240';
    expect(optionalPositiveIntFromEnv(NAME)).toBe(240);
  });

  test.each(['0', '-1', '1e3', '0x80', ' 240 ', '2.0'])(
    'still rejects %s',
    (bad) => {
      // The container reads these with Python's int(), which accepts a different
      // language; a knob that means two things is worse than one that is rejected.
      process.env[NAME] = bad;
      expect(() => optionalPositiveIntFromEnv(NAME)).toThrow(NAME);
    },
  );
});
