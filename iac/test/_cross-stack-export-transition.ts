/**
 * The cross-stack export/import check that would have caught B1 (round-1
 * implementation review, both Fable's and Sol's matching blocker): going
 * straight from `vpc-switch` to `vpc` had AlbStack remove the public ALB's
 * exports in the SAME deploy where FrontendStack/EcsStack still imported
 * them. `cdk deploy --all` always deploys a producer stack (the one an
 * `Fn::ImportValue` points INTO) before a consumer that depends on it, so a
 * producer trying to delete an export the CURRENTLY-DEPLOYED consumer
 * template still imports is refused by CloudFormation outright ("Export ...
 * cannot be deleted as it is in use by ...") — regardless of what the
 * consumer's OWN pending update would do, because CloudFormation checks the
 * export's usage against what is deployed NOW, not against the update that
 * has not run yet.
 *
 * Two checks, both pure functions over synthesized CloudFormation JSON —
 * no AWS calls, no live account:
 *
 *   1. `unresolvedImports`: every `Fn::ImportValue` anywhere in the NEW
 *      state's own templates must resolve to an export some stack in that
 *      SAME state actually declares. A completeness check on one state by
 *      itself — if this fails, the synthesized NEW state cannot deploy at
 *      all, let alone migrate safely into.
 *   2. `unsafeRemovals`: every export present in the OLD state but absent
 *      from the NEW state (the producer stopped declaring it) must not be
 *      imported by ANY template in the OLD state. This is the actual
 *      deadlock detector — it is deliberately checked against OLD's
 *      imports, not NEW's: the moment `cdk deploy --all` tries to delete
 *      the export, every OTHER stack in the deployment is still running
 *      its OLD template (that is what "still imported" means to
 *      CloudFormation at that instant), so an export OLD's own consumers
 *      relied on cannot be removed safely in the SAME transition, no matter
 *      what the NEW consumer template would have looked like once it also
 *      finished updating.
 */

export type StackTemplate = {
  Resources?: Record<string, any>;
  Outputs?: Record<string, any>;
};

export type StackSet = Record<string, StackTemplate>;

export interface ExportTransitionProblems {
  /** `Fn::ImportValue` targets in the NEW state with no matching export anywhere in the NEW state. */
  unresolvedImports: string[];
  /** Exports the transition removes that the OLD state's own templates still import. */
  unsafeRemovals: string[];
}

/** Every `Export.Name` string declared anywhere across a stack set's `Outputs`. */
export function exportNames(stacks: StackSet): Set<string> {
  const names = new Set<string>();
  for (const tpl of Object.values(stacks)) {
    for (const output of Object.values(tpl.Outputs || {})) {
      const exportName = (output as { Export?: { Name?: unknown } })?.Export?.Name;
      if (typeof exportName === 'string') {
        names.add(exportName);
      }
      // A non-string Export.Name (an unresolved intrinsic such as
      // `Fn::Sub`) is not something this checker can statically correlate
      // across two independent synths — CDK's own auto-generated
      // cross-stack export names ARE plain strings (deterministic from the
      // producer stack name + logical id + attribute, not an unresolved
      // token), which is the only kind this codebase's cross-stack
      // references ever produce; a future change introducing a literal
      // `exportName` built from an intrinsic would silently stop being
      // checked here rather than crash, which is the same posture
      // `test_claims_are_anchored.py`'s own "a false negative is the thing
      // to worry about" framing takes for an analogous gap.
    }
  }
  return names;
}

/** Every `Fn::ImportValue` target found anywhere (however deeply nested) in a stack set's `Resources`/`Outputs`. */
export function allImports(stacks: StackSet): string[] {
  const found: string[] = [];
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) {
      for (const item of node) walk(item);
    } else if (node && typeof node === 'object') {
      for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
        if (key === 'Fn::ImportValue' && typeof value === 'string') {
          found.push(value);
        } else {
          walk(value);
        }
      }
    }
  };
  for (const tpl of Object.values(stacks)) {
    walk(tpl.Resources);
    walk(tpl.Outputs);
  }
  return found;
}

/**
 * Check one transition, OLD state -> NEW state, for the two problems above.
 * Both `oldStacks` and `newStacks` are the FULL set of templates for their
 * respective state — a partial set (missing a producer or consumer stack)
 * would silently under-report, so callers must pass every stack `bin/iac.ts`
 * builds for that `cloudfrontOrigin`/`entryMode` combination.
 */
export function checkExportTransition(
  oldStacks: StackSet,
  newStacks: StackSet,
): ExportTransitionProblems {
  const newExports = exportNames(newStacks);
  const newImports = allImports(newStacks);
  const unresolvedImports = Array.from(
    new Set(newImports.filter((imp) => !newExports.has(imp))),
  );

  const oldExports = exportNames(oldStacks);
  const removedExports = Array.from(oldExports).filter((e) => !newExports.has(e));
  const oldImports = new Set(allImports(oldStacks));
  const unsafeRemovals = removedExports.filter((e) => oldImports.has(e));

  return { unresolvedImports, unsafeRemovals };
}

/** `true` when a transition has neither problem — the shape callers assert on in the common case. */
export function isSafeTransition(problems: ExportTransitionProblems): boolean {
  return problems.unresolvedImports.length === 0 && problems.unsafeRemovals.length === 0;
}
