# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-17

### Added

- `weak_assert.py`: zero-dependency AST audit for pytest / unittest suites.
- Rules WA001–WA008: assertion-free tests, constant assertions, bare truthiness,
  over-broad exceptions, swallowed assertions, self-comparisons, dead
  assertions, skip without reason.
- Inline (`# weak-assert: ignore[=RULE]`) and file-level suppression.
- Text, Markdown and JSON reports; `--fail-on` exit codes for CI.
- 61 unit tests, GitHub Actions matrix (Python 3.9–3.12), banner and demo SVG.

### Fixed

Issues found by adversarial verification before the first release:

- Skip idioms (`pytest.skip`, `self.skipTest`, `raise unittest.SkipTest`) were
  counted as assertion-free tests.
- `assert x == 1 and y == 2` and `assert not isinstance(...)` were reported as
  truthiness-only.
- `except ValueError` was treated as swallowing an `AssertionError`, and
  handlers containing `self.assertEqual` were treated as swallowing.
- `assertTrue(True)`, `assertTrue([])` and literal containers were not reported
  as constant assertions.
- BOM-prefixed files were skipped; NUL bytes crashed the parser.
- `unittest.skipIf(cond, "reason")` was reported as missing a reason.
- `# weak-assert: ignore-file` was ignored after line 5.
- `subTest` was treated as an assertion.
- WA007 only looked at the statement immediately after `return`.
- Report paths that were directories crashed instead of exiting 2.
