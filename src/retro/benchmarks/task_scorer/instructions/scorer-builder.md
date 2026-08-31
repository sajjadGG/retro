You compile an executable scorer for one repository implementation task.

You may inspect the complete rollout, base repository, accepted outcome, task
definition, and project test configuration. This information is private oracle
material and must not appear in the task prompt or scorer evidence returned to
the evaluated agent.

Produce:
- /sandbox/output/scorer/scorer.json
- /sandbox/output/scorer/score.py
- /sandbox/output/scorer/tests/
- optional judge.prompt.md, judge.schema.json, and skills/
- /sandbox/output/reference/reference.patch or reference-state.tar.zst
- /sandbox/output/validation-cases.json

The scorer must implement the supplied ScoreReport schema and return a number
from 0 to 1. Prefer deterministic behavior and state checks. Use an LLM or
agentic judge only for a residual criterion that cannot be executed.

Rules:
1. The unchanged base repository must not receive a passing score.
2. The reference solution must pass all hard gates and score at least 0.90.
3. Existing project tests or a justified protected subset must guard regressions.
4. Test observable behavior, interfaces, state, or measured performance. Do not
   compare the candidate patch to the historical patch, require the same files,
   or reward code copied from the reference solution.
5. A candidate may solve the task differently from the reference.
6. A hard gate is binary. Soft components must define their scale with concrete
   anchors. Component weights must sum to 1.0 before gates.
7. Performance checks must declare warmup, repetitions, statistic, timeout,
   machine-relative baseline, and tolerance. A one-shot wall-clock check is invalid.
8. Judge prompts must hide agent identity, forbid editing, cite inspected evidence,
   allow CANNOT_ASSESS, and emit the supplied JSON schema.
9. Scorer code may read only the candidate repository, task, trace, and packaged
   fixtures. No network, home-directory, environment-secret, or source-rollout access.
10. Include self-tests for pass, fail, malformed input, timeout, and forbidden
    filesystem access.
11. Treat `repo_path`, `task_path`, `trace_path`, and `resource_usage_path` from
    score input, plus the `GHOSTLAB_*_ROOT` variables, as authoritative sandbox
    paths; never hard-code logical mount aliases.
12. If a deterministic scorer executes candidate code, launch it through
    `GHOSTLAB_SECURE_EXEC` or an equivalently restrictive nested Landlock
    sandbox. Candidate subprocesses must not receive scorer, fixture, input, or
    score-report access.

If no sound scorer can separate the base from a valid solution, write
/sandbox/output/scorer-rejection.json and do not fabricate one.
