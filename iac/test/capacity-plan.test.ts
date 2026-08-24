import { capacityPlan, workersForCpuUnits } from '../lib/_common';

describe('capacityPlan', () => {
  const base = {
    target: 1024,
    minTasks: 2,
    maxTasks: 8,
    perProcessRequests: 32,
    workersPerTask: 4,
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

  test('counts processes, not tasks', () => {
    // Latency tracks requests in flight per process, so four workers admit four
    // times what one does at the same per-process ceiling.
    const oneWorker = capacityPlan({ ...base, workersPerTask: 1 });
    expect(oneWorker.sustained).toBe(256);
    expect(capacityPlan({ ...base, workersPerTask: 8 }).sustained).toBe(2048);
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

describe('workersForCpuUnits', () => {
  test('a one-vCPU task stays single-process, as it was', () => {
    expect(workersForCpuUnits(1024)).toBe(1);
  });

  test('a larger task gets the workers its cores can run', () => {
    // Each process has its own GIL, which is the point; a worker with no core to
    // run on would gain nothing.
    expect(workersForCpuUnits(4096)).toBe(4);
    expect(workersForCpuUnits(16384)).toBe(16);
  });

  test('a fractional-vCPU task still gets one worker', () => {
    expect(workersForCpuUnits(256)).toBe(1);
    expect(workersForCpuUnits(512)).toBe(1);
  });

  test('workers never exceed the vCPU count', () => {
    // 3 vCPU is not a Fargate size, but the floor must not round up into cores
    // that do not exist.
    expect(workersForCpuUnits(3584)).toBe(3);
  });
});
