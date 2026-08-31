You independently audit one generated repository-task scorer. Do not edit the
task, scorer, reference solution, or supplied validation results.

Verify that:
- every task acceptance claim maps to at least one scorer component;
- the unchanged base, no-op, construct-changing, and regression cases fail as
  required, while the oracle and construct-preserving cases pass;
- behaviorally valid alternatives are not penalized for patch shape, file
  location, symbol naming, formatting, or resemblance to the accepted change;
- hidden fixtures remain outside the candidate mount and candidate code cannot
  modify scorer inputs, tests, fixtures, or reports;
- the public prompt contains no outcome-only or scorer-only information;
- scorer behavior is independent of the candidate agent's identity and prose;
- deterministic and judge components satisfy their repeatability thresholds.

You may add adversarial candidate mutants only under /sandbox/output/mutants.
When scorer code executes candidate code, verify that the child runs through
`GHOSTLAB_SECURE_EXEC` or an equivalent nested sandbox without access to scorer
files, hidden fixtures, task input, or score-report output.
Write exactly one decision record to /sandbox/output/audit.json with decision
accept, revise, or reject and arrays for leakage, overfit_checks,
missing_observables, mutants, and evidence. Accept only when every required
check is supported by cited repository paths, scorer components, or validation
executions.
