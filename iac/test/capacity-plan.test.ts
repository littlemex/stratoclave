import { capacityPlan } from '../lib/_common';

describe('capacityPlan', () => {
  const base = {
    target: 1024,
    minTasks: 2,
    maxTasks: 8,
    perTaskRequests: 128,
    requestsPerTarget: 240,
  };

  test('reports what serves a burst and what needs a scale-out', () => {
    // These are different numbers and conflating them is how a fleet ends up
    // nominally sized for a target it never reaches.
    const plan = capacityPlan(base);
    expect(plan.immediate).toBe(256);
    expect(plan.sustained).toBe(1024);
    expect(plan.warnings).toEqual([]);
    expect(plan.notes.join(' ')).toMatch(/waits for a scale-out/);
  });

  test('a floor sized for the target reports no scale-out caveat', () => {
    const plan = capacityPlan({ ...base, minTasks: 8 });
    expect(plan.immediate).toBe(1024);
    expect(plan.notes.join(' ')).not.toMatch(/waits for a scale-out/);
  });

  test('warns when the ceiling cannot reach the target at all', () => {
    const plan = capacityPlan({ ...base, maxTasks: 4 });
    expect(plan.sustained).toBe(512);
    expect(plan.warnings.join(' ')).toMatch(/cannot reach its concurrency target/);
  });

  test('warns when the fleet may grow but nothing will grow it', () => {
    // CPU tracking stays low on this workload while it saturates, so a fleet with
    // no request-count budget has a ceiling it will never climb to.
    const plan = capacityPlan({ ...base, requestsPerTarget: undefined });
    expect(plan.warnings.join(' ')).toMatch(/nothing will grow it except CPU/);
  });

  test('a fixed-size fleet needs no growth signal', () => {
    const plan = capacityPlan({
      ...base,
      minTasks: 8,
      maxTasks: 8,
      requestsPerTarget: undefined,
    });
    expect(plan.warnings).toEqual([]);
  });
});
