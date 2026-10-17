---
name: weak-assert
description: Audit Python pytest or unittest suites for tests that run code but never really check it. Use when asked to review test quality, find assertion-free or always-true tests, explain why a suite passes despite bugs, or gate CI on assertion quality. Reports line-addressable findings; it does not execute tests or rewrite code.
---

# weak-assert

Use this skill when the question is **"are these tests checking anything?"**,
not "how much code do they touch?". Coverage tells you what ran; weak-assert
tells you whether the result was actually compared to an expectation.

## Quick start

```bash
python3 scripts/weak_assert.py tests
python3 scripts/weak_assert.py tests --fail-on high
python3 scripts/weak_assert.py tests --json wa.json --md wa.md
```

Default target is `tests/`. Pass files or directories as positional arguments.
`--list-rules` prints the rule table.

## Rules

| Rule | Meaning | Default |
| --- | --- | --- |
| WA001 | test body has no assertion | medium |
| WA002 | assertion on a literal / identical literals | medium |
| WA003 | bare truthiness assertion (`assert x`) | low |
| WA004 | `pytest.raises(Exception)` / `assertRaises(BaseException)` | medium |
| WA005 | assertion inside `try`, handler does not re-raise | high |
| WA006 | `x == x`, `assertEqual(x, x)` | high |
| WA007 | assertion after an unconditional return/raise | medium |
| WA008 | skip/xfail decorator without a reason | low |

## How to triage

1. Start with `WA005` and `WA006`: these hide real failures.
2. `WA001` is the headline count; check whether the test calls a helper that
   asserts internally before changing it (the scanner cannot see that).
3. `WA002` on `assert True` or `assertEqual(1, 1)` is usually a stub.
4. `WA003` is advisory. Replace `assert result` with a comparison only when an
   expected value exists.
5. `WA004` is a design signal: expect the narrow exception the code raises.
6. `WA008` is about future readers; add `reason="..."`.

Do not bulk-rewrite tests to silence the report. A suppressed bad test is worse
than a reported one.

## Suppression

```python
def test_legacy():  # weak-assert: ignore=WA003
    assert result
```

`# weak-assert: ignore` (no rule list) suppresses all rules on that line.
`# weak-assert: ignore-file` in a comment disables the whole file. The marker
must be in a comment — a string literal containing it does not count.

## Exit codes and outputs

- `--fail-on high|medium|low` → exit 1 when a finding at or above that level exists.
- `--json FILE` / `--md FILE` write structured reports.
- `--ignore WA003,WA008` drops rules before reporting and thresholding.
- Exit 2 on a missing path, unknown rule, or unwritable report path.

## Guardrails

- Static analysis only: no test execution, no coverage measurement, no network.
- Python only: pytest and unittest conventions. JS/TS tests are not scanned.
- Cannot resolve helper assertions, dynamic `getattr` assertions, or assertion
  libraries that do not expose `assert*` names.
- Never edits files and never claims a test is behaviorally correct.
- `WA003` is deliberately conservative; `assertTrue(x)` is not reported.

## Requirements

Python 3.9+, standard library only.
