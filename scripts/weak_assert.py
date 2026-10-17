#!/usr/bin/env python3
"""weak-assert — find tests that run code but never really check it.

Zero dependencies, Python 3.9+. Audits pytest / unittest test suites with the
standard library AST. It reports mechanical, line-addressable problems:

    WA001  test contains no assertion at all
    WA002  constant assertion (always passes, or always fails)
    WA003  truthiness-only assertion (no comparison)
    WA004  over-broad expected exception
    WA005  assertion swallowed by try/except
    WA006  self-comparison (x == x)
    WA007  assertion after an unconditional return (dead code)
    WA008  skipped/xfail test without a reason

It does not run tests, measure coverage, or claim that a test is "good".
See SKILL.md for the exact boundaries.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

VERSION = "1.0.0"

# rule -> (default severity, one-line title)
RULES: Dict[str, Tuple[str, str]] = {
    "WA001": ("medium", "test contains no assertion"),
    "WA002": ("medium", "constant assertion"),
    "WA003": ("low", "bare truthiness assertion"),
    "WA004": ("medium", "over-broad expected exception"),
    "WA005": ("high", "assertion swallowed by try/except"),
    "WA006": ("high", "self-comparison (x == x)"),
    "WA007": ("medium", "assertion after unconditional return"),
    "WA008": ("low", "skip/xfail without a reason"),
}

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}

DEFAULT_EXCLUDES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
    "node_modules", "build", "dist", ".tox", ".nox", ".mypy_cache",
    ".pytest_cache", "site-packages",
}

TEST_NAME_RE = re.compile(r"^test(?:_|$|[A-Z])")
SUPPRESS_RE = re.compile(r"#\s*weak-assert:\s*ignore(?:=([A-Za-z0-9,\s]+))?")
FILE_IGNORE_RE = re.compile(r"#\s*weak-assert:\s*ignore-file\b")

ASSERT_METHODS = {
    "assertEqual", "assertEquals", "assertNotEqual", "assertNotEquals",
    "assertTrue", "assertFalse", "assertIs", "assertIsNot", "assertIsNone",
    "assertIsNotNone", "assertIn", "assertNotIn", "assertIsInstance",
    "assertNotIsInstance", "assertRaises", "assertRaisesRegex",
    "assertRaisesRegexp", "assertAlmostEqual", "assertNotAlmostEqual",
    "assertGreater", "assertGreaterEqual", "assertLess", "assertLessEqual",
    "assertRegex", "assertNotRegex", "assertCountEqual", "assertListEqual",
    "assertDictEqual", "assertSetEqual", "assertTupleEqual", "assertMultiLineEqual",
    "assertLogs", "assertWarns", "assertWarnsRegex", "fail", "failUnless",
    "failIf", "failUnlessEqual", "failIfEqual", "failUnlessRaises",
    "raises", "warns",
}

BROAD_EXCEPTIONS = {"Exception", "BaseException", "Throwable"}

WEAK_NODE_TYPES = (ast.Name, ast.Attribute, ast.Subscript)


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    column: int
    symbol: str
    message: str
    snippet: str
    extra: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        data = {
            "rule": self.rule,
            "severity": self.severity,
            "file": self.path,
            "line": self.line,
            "column": self.column,
            "symbol": self.symbol,
            "message": self.message,
            "snippet": self.snippet,
        }
        data.update(self.extra)
        return data


@dataclass
class FileResult:
    path: str
    findings: List[Finding] = field(default_factory=list)
    tests: int = 0
    tests_with_assertions: int = 0
    tests_skipped: int = 0
    parsed: bool = True


# --------------------------------------------------------------------------
# file discovery and suppression
# --------------------------------------------------------------------------

def iter_python_files(paths: Sequence[str], excludes: Set[str]) -> Iterable[Path]:
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            if p.suffix == ".py":
                yield p
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if d not in excludes and not d.startswith("."))
            for name in sorted(files):
                if name.endswith(".py"):
                    yield Path(root) / name


def parse_suppressions(lines: List[str]) -> Tuple[Dict[int, Set[str]], bool]:
    """Return (line -> rules or {'*'}, file_ignored)."""
    per_line: Dict[int, Set[str]] = {}
    file_ignored = False
    for idx, text in enumerate(lines, 1):
        if text.lstrip().startswith("#") and FILE_IGNORE_RE.search(text):
            file_ignored = True
        m = SUPPRESS_RE.search(text)
        if not m:
            continue
        raw = m.group(1)
        if not raw or not raw.strip():
            per_line.setdefault(idx, set()).add("*")
        else:
            rules = {part.strip().upper() for part in raw.split(",") if part.strip()}
            per_line.setdefault(idx, set()).update(rules)
    return per_line, file_ignored


def suppressed(suppressions: Dict[int, Set[str]], lines: Iterable[int], rule: str) -> bool:
    for line in lines:
        rules = suppressions.get(line)
        if rules and ("*" in rules or rule in rules):
            return True
    return False


# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------

def call_name(node: ast.AST) -> str:
    """Best-effort dotted name of a Call/Attribute target, e.g. pytest.raises."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return call_name(node.func)
    return ""


def func_attr(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def iter_without_nested(node: ast.AST) -> Iterable[ast.AST]:
    """Walk a function body but do not descend into nested defs/classes."""
    stack: List[ast.AST] = list(getattr(node, "body", []))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield current
        stack.extend(ast.iter_child_nodes(current))


def is_assertion(node: ast.AST, in_with: bool = False) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, ast.Call):
        attr = func_attr(node)
        if attr in ASSERT_METHODS:
            return True
        if attr.startswith("assert"):
            # mock.assert_called_once_with(...) and friends assert behaviour.
            return True
    if isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            expr = item.context_expr
            if isinstance(expr, ast.Call) and func_attr(expr) in {"raises", "warns", "assertRaises", "assertWarns", "assertRaisesRegex", "assertWarnsRegex"}:
                return True
    return False


def has_assertion(body: Sequence[ast.AST]) -> bool:
    for node in body:
        if is_assertion(node):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if has_assertion(list(ast.iter_child_nodes(node))):
            return True
    return False


def ast_equal(left: ast.AST, right: ast.AST) -> bool:
    try:
        return ast.dump(left, annotate_fields=False) == ast.dump(right, annotate_fields=False)
    except Exception:
        return False


def is_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant)


def constant_assertion(node: ast.Assert) -> bool:
    return isinstance(node.test, ast.Constant)


def constants_equal_call(node: ast.Call) -> bool:
    attr = func_attr(node)
    if attr not in {"assertEqual", "assertEquals", "assertIs", "failUnlessEqual", "failIfEqual"}:
        return False
    if len(node.args) < 2:
        return False
    left, right = node.args[0], node.args[1]
    if not (is_constant(left) and is_constant(right)):
        return False
    return repr(getattr(left, "value", None)) == repr(getattr(right, "value", None))


def broad_exception(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in BROAD_EXCEPTIONS
    if isinstance(node, ast.Tuple):
        return any(broad_exception(elt) for elt in node.elts)
    return False


def expected_exception_nodes(node: ast.AST) -> Iterable[ast.AST]:
    if isinstance(node, ast.Call) and func_attr(node) in {
        "raises", "assertRaises", "assertRaisesRegex", "assertRaisesRegexp", "assertWarns", "assertWarnsRegex"
    }:
        if node.args:
            yield node.args[0]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            expr = item.context_expr
            if isinstance(expr, ast.Call) and func_attr(expr) in {"raises", "assertRaises", "assertRaisesRegex", "assertRaisesRegexp", "assertWarns", "assertWarnsRegex"}:
                if expr.args:
                    yield expr.args[0]


def handler_swallows(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Raise):
            return False
        if is_assertion(node):
            return False
    return True


def handler_catches_assertion(handler: ast.ExceptHandler) -> bool:
    """Only handlers that can actually intercept AssertionError matter."""
    return _type_catches_assertion(handler.type)


def _type_catches_assertion(node: Optional[ast.AST]) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in {"Exception", "BaseException", "AssertionError"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"Exception", "BaseException", "AssertionError"}
    if isinstance(node, ast.Tuple):
        return any(_type_catches_assertion(elt) for elt in node.elts)
    return False


def is_skip_node(node: ast.AST) -> bool:
    if isinstance(node, ast.Raise):
        exc = node.exc
        if isinstance(exc, ast.Call):
            return func_attr(exc) in {"SkipTest"}
        if isinstance(exc, ast.Name):
            return exc.id == "SkipTest"
        return False
    if isinstance(node, ast.Call):
        attr = func_attr(node)
        name = call_name(node.func)
        return attr in {"skip", "skipTest", "importorskip"} or name in {"pytest.skip", "unittest.skip"}
    return False


def is_weak_value(node: ast.AST) -> bool:
    return isinstance(node, WEAK_NODE_TYPES)


def skip_reason_missing(dec: ast.AST) -> bool:
    """True when a skip/xfail decorator carries no human-readable reason."""
    if not isinstance(dec, ast.Call):
        return func_attr(dec) in {"skip", "skipIf", "skipUnless", "xfail"}
    name = call_name(dec.func)
    if not (name.endswith("mark.skip") or name.endswith("mark.skipif") or name.endswith("mark.xfail")
            or name in {"skip", "skipIf", "skipUnless", "unittest.skip", "unittest.skipIf", "unittest.skipUnless"}):
        return False
    for kw in dec.keywords:
        if kw.arg == "reason":
            # Any provided reason counts, including a variable or constant.
            if isinstance(kw.value, ast.Constant) and not str(kw.value.value or "").strip():
                continue
            return False
    # unittest.skip("reason") passes the reason positionally. A name/variable
    # in that slot is still a reason, so only an empty constant is "missing".
    if name in {"unittest.skip", "unittest.skipIf", "unittest.skipUnless", "skip", "skipIf", "skipUnless"}:
        for arg in dec.args:
            if isinstance(arg, ast.Constant):
                if isinstance(arg.value, str) and arg.value.strip():
                    return False
            else:
                return False
    return True


def contains_assertion(nodes: Sequence[ast.AST]) -> bool:
    return any(is_assertion(n) for n in iter_without_nested(ast.Module(body=list(nodes), type_ignores=[])))


def assign_qualname(node: ast.AST, parents: Dict[ast.AST, ast.AST]) -> str:
    parts = [getattr(node, "name", "<lambda>")]
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.ClassDef):
            parts.append(parent.name)
        parent = parents.get(parent)
    return ".".join(reversed(parts))


def build_parents(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    parents: Dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def analyze_python(path: Path, display: str) -> FileResult:
    result = FileResult(path=display)
    try:
        source = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        print(f"weak-assert: cannot read {display}: {exc}", file=sys.stderr)
        result.parsed = False
        return result

    lines = source.splitlines()
    suppressions, ignored = parse_suppressions(lines)
    if ignored:
        return result
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError) as exc:
        msg = getattr(exc, "msg", str(exc))
        line = getattr(exc, "lineno", "?")
        print(f"weak-assert: skipping {display}: {msg} (line {line})", file=sys.stderr)
        result.parsed = False
        return result

    parents = build_parents(tree)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Only real test entry points, never nested helpers.
        parent = parents.get(node)
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not TEST_NAME_RE.match(node.name):
            continue
        symbol = assign_qualname(node, parents)
        result.tests += 1
        body_nodes = list(iter_without_nested(node))
        assertion_found = any(is_assertion(n) for n in body_nodes)
        skip_only = not assertion_found and any(is_skip_node(n) for n in body_nodes)
        if assertion_found:
            result.tests_with_assertions += 1
        elif skip_only:
            result.tests_skipped += 1

        def add(rule: str, line: int, column: int, message: str, extra: Optional[Dict[str, object]] = None) -> None:
            if suppressed(suppressions, [line, node.lineno], rule):
                return
            snippet = lines[line - 1].strip() if 0 < line <= len(lines) else ""
            result.findings.append(Finding(
                rule=rule,
                severity=RULES[rule][0],
                path=display,
                line=line,
                column=column,
                symbol=symbol,
                message=message,
                snippet=snippet,
                extra=extra or {},
            ))

        if not assertion_found and not skip_only:
            add("WA001", node.lineno, node.col_offset + 1,
                "test body has no assertion; it only exercises code")

        assertion_count = 0
        for inner in body_nodes:
            if isinstance(inner, ast.Assert):
                assertion_count += 1
                if constant_assertion(inner):
                    add("WA002", inner.lineno, inner.col_offset + 1,
                        "assertion on a literal constant can never detect a regression"
                        if getattr(inner.test, "value", None) else
                        "assertion on a falsy constant always fails")
                elif isinstance(inner.test, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                    add("WA002", inner.lineno, inner.col_offset + 1,
                        "assertion on a literal container is constant")
                elif isinstance(inner.test, WEAK_NODE_TYPES):
                    add("WA003", inner.lineno, inner.col_offset + 1,
                        "assertion only checks truthiness; compare against the expected value")
                elif isinstance(inner.test, ast.BoolOp):
                    if all(is_weak_value(value) for value in inner.test.values):
                        add("WA003", inner.lineno, inner.col_offset + 1,
                            "assertion only checks truthiness of boolean expressions")
                elif isinstance(inner.test, ast.UnaryOp) and isinstance(inner.test.op, ast.Not):
                    if is_weak_value(inner.test.operand):
                        add("WA003", inner.lineno, inner.col_offset + 1,
                            "assertion only checks truthiness; compare against the expected value")
                elif isinstance(inner.test, ast.Compare):
                    values = [inner.test.left] + list(inner.test.comparators)
                    if all(is_constant(v) for v in values):
                        add("WA002", inner.lineno, inner.col_offset + 1,
                            "comparison between literals can never detect a regression")

            if isinstance(inner, ast.Call):
                if constants_equal_call(inner):
                    add("WA002", inner.lineno, inner.col_offset + 1,
                        f"{func_attr(inner)} compares two identical literals")
                attr = func_attr(inner)
                if attr in {"assertTrue", "assertFalse", "failUnless", "failIf"} and len(inner.args) == 1:
                    arg = inner.args[0]
                    if is_constant(arg) or isinstance(arg, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                        add("WA002", inner.lineno, inner.col_offset + 1,
                            f"{attr} on a literal is a constant assertion")
                attr = func_attr(inner)
                if attr in {"assertEqual", "assertEquals", "assertIs"} and len(inner.args) >= 2:
                    if not (is_constant(inner.args[0]) and is_constant(inner.args[1])) \
                            and ast_equal(inner.args[0], inner.args[1]):
                        add("WA006", inner.lineno, inner.col_offset + 1,
                            f"{attr} compares a value with itself")

            if isinstance(inner, ast.Compare) and len(inner.ops) == 1 and isinstance(inner.ops[0], (ast.Eq, ast.Is)):
                if ast_equal(inner.left, inner.comparators[0]) and not is_constant(inner.left):
                    add("WA006", inner.lineno, inner.col_offset + 1,
                        "comparison of a value with itself is always true")

            for exc_node in expected_exception_nodes(inner):
                if broad_exception(exc_node):
                    add("WA004", inner.lineno, inner.col_offset + 1,
                        "expecting a broad exception makes the test pass for the wrong failure")

            if isinstance(inner, ast.Try):
                if contains_assertion(inner.body):
                    for handler in inner.handlers:
                        if handler_catches_assertion(handler) and handler_swallows(handler):
                            add("WA005", handler.lineno, handler.col_offset + 1,
                                "except handler swallows the assertion failure without re-raising")

        # Unreachable assertions after an unconditional return/raise.
        for idx, stmt in enumerate(node.body[:-1]):
            if isinstance(stmt, (ast.Return, ast.Raise)):
                for trailing in node.body[idx + 1:]:
                    if contains_assertion([trailing]):
                        line = getattr(trailing, "lineno", stmt.lineno)
                        add("WA007", line, getattr(trailing, "col_offset", 0) + 1,
                            "statement (including its assertion) is unreachable after an unconditional exit")
                        break
                break

        for dec in node.decorator_list:
            if skip_reason_missing(dec):
                add("WA008", getattr(dec, "lineno", node.lineno), getattr(dec, "col_offset", 0) + 1,
                    "skip/xfail decorator has no reason; future readers cannot tell why it is off")

    # Bare skip/xfail decorators on test classes are also worth a note.
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and TEST_NAME_RE.match(node.name):
            for dec in node.decorator_list:
                if skip_reason_missing(dec) and not suppressed(
                        suppressions, [getattr(dec, "lineno", node.lineno), node.lineno], "WA008"):
                    line = getattr(dec, "lineno", node.lineno)
                    result.findings.append(Finding(
                        rule="WA008",
                        severity=RULES["WA008"][0],
                        path=display,
                        line=line,
                        column=getattr(dec, "col_offset", 0) + 1,
                        symbol=node.name,
                        message="skip/xfail decorator on a test class has no reason",
                        snippet=lines[line - 1].strip() if 0 < line <= len(lines) else "",
                    ))
    return result


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def dedupe(findings: List[Finding]) -> List[Finding]:
    seen: Set[Tuple[str, str, int, str]] = set()
    out: List[Finding] = []
    for f in findings:
        key = (f.rule, f.path, f.line, f.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def sort_findings(findings: List[Finding]) -> List[Finding]:
    return sorted(findings, key=lambda f: (-SEVERITY_RANK[f.severity], f.path, f.line, f.rule))


def summarize(results: List[FileResult]) -> Dict[str, object]:
    findings = [f for r in results for f in r.findings]
    by_severity = {level: 0 for level in ("high", "medium", "low")}
    by_rule: Dict[str, int] = {}
    for f in findings:
        by_severity[f.severity] += 1
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
    tests = sum(r.tests for r in results)
    with_assertions = sum(r.tests_with_assertions for r in results)
    skipped = sum(r.tests_skipped for r in results)
    return {
        "files_scanned": sum(1 for r in results if r.parsed),
        "files_skipped": sum(1 for r in results if not r.parsed),
        "tests_scanned": tests,
        "tests_with_assertions": with_assertions,
        "tests_skipped": skipped,
        "assertion_free_tests": tests - with_assertions - skipped,
        "findings": len(findings),
        "by_severity": by_severity,
        "by_rule": dict(sorted(by_rule.items())),
    }


def render_text(results: List[FileResult], summary: Dict[str, object]) -> str:
    out: List[str] = []
    findings = sort_findings([f for r in results for f in r.findings])
    out.append(
        f"weak-assert {VERSION}: scanned {summary['tests_scanned']} tests in "
        f"{summary['files_scanned']} files; {summary['assertion_free_tests']} have no assertion."
    )
    if findings:
        out.append("")
        for f in findings:
            out.append(f"{f.severity.upper():<6} {f.rule}  {f.path}:{f.line}  {f.symbol}")
            out.append(f"       {f.message}")
            if f.snippet:
                out.append(f"       | {f.snippet}")
    counts = summary["by_severity"]
    out.append("")
    out.append(f"{summary['findings']} findings "
               f"({counts['high']} high, {counts['medium']} medium, {counts['low']} low)")
    return "\n".join(out)


def render_markdown(results: List[FileResult], summary: Dict[str, object]) -> str:
    findings = sort_findings([f for r in results for f in r.findings])
    lines: List[str] = []
    lines.append("# weak-assert report")
    lines.append("")
    lines.append(
        f"Scanned **{summary['tests_scanned']}** test functions in "
        f"**{summary['files_scanned']}** files. "
        f"**{summary['assertion_free_tests']}** contain no assertion."
    )
    lines.append("")
    counts = summary["by_severity"]
    lines.append(f"Findings: **{summary['findings']}** "
                 f"({counts['high']} high / {counts['medium']} medium / {counts['low']} low)")
    lines.append("")
    if findings:
        lines.append("| Severity | Rule | Location | Test | Finding |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in findings:
            location = f"`{f.path.replace('|', chr(92) + '|')}:{f.line}`"
            message = f.message.replace("|", "\\|")
            lines.append(f"| {f.severity} | {f.rule} | {location} | `{f.symbol}` | {message} |")
    else:
        lines.append("No findings.")
    if summary["by_rule"]:
        lines.append("")
        lines.append("## By rule")
        lines.append("")
        lines.append("| Rule | Count | Meaning |")
        lines.append("| --- | --- | --- |")
        for rule, count in summary["by_rule"].items():
            lines.append(f"| {rule} | {count} | {RULES.get(rule, ('', ''))[1]} |")
    lines.append("")
    return "\n".join(lines)


def write_text(path: str, text: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weak-assert",
        description="Audit Python tests for assertions that cannot fail.",
    )
    parser.add_argument("paths", nargs="*", help="files or directories to scan (default: tests/)")
    parser.add_argument("--json", metavar="FILE", help="write a machine-readable JSON report")
    parser.add_argument("--md", metavar="FILE", help="write a Markdown report")
    parser.add_argument("--ignore", default="", metavar="RULES",
                        help="comma-separated rules to skip, e.g. WA003,WA008")
    parser.add_argument("--exclude", action="append", default=[], metavar="NAME",
                        help="directory name to skip (repeatable)")
    parser.add_argument("--fail-on", choices=["high", "medium", "low"], default=None,
                        help="exit 1 when a finding at or above this severity exists")
    parser.add_argument("--list-rules", action="store_true", help="print the rule table and exit")
    parser.add_argument("--quiet", action="store_true", help="suppress the text report")
    parser.add_argument("--version", action="version", version=f"weak-assert {VERSION}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_rules:
        for rule, (severity, title) in RULES.items():
            print(f"{rule}  {severity:<6}  {title}")
        return 0

    paths = args.paths or ["tests"]
    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        print(f"weak-assert: path not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    ignored = {r.strip().upper() for r in args.ignore.split(",") if r.strip()}
    unknown = ignored - set(RULES)
    if unknown:
        print(f"weak-assert: unknown rule(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    excludes = set(DEFAULT_EXCLUDES)
    for name in args.exclude:
        excludes.add(name)

    results: List[FileResult] = []
    for path in iter_python_files(paths, excludes):
        try:
            rel = os.path.relpath(path, Path.cwd())
            display = str(path) if rel.startswith("..") else rel
        except ValueError:
            display = str(path)
        result = analyze_python(path, display)
        result.findings = dedupe([f for f in result.findings if f.rule not in ignored])
        results.append(result)

    summary = summarize(results)
    text = render_text(results, summary)
    if not args.quiet:
        print(text)
    if args.md:
        try:
            write_text(args.md, render_markdown(results, summary))
        except OSError as exc:
            print(f"weak-assert: cannot write {args.md}: {exc}", file=sys.stderr)
            return 2
        print(f"weak-assert: wrote {args.md}", file=sys.stderr)
    if args.json:
        payload = {
            "tool": "weak-assert",
            "version": VERSION,
            "summary": summary,
            "findings": [f.as_dict() for f in sort_findings([f for r in results for f in r.findings])],
        }
        try:
            write_text(args.json, json.dumps(payload, indent=2, ensure_ascii=False))
        except OSError as exc:
            print(f"weak-assert: cannot write {args.json}: {exc}", file=sys.stderr)
            return 2
        print(f"weak-assert: wrote {args.json}", file=sys.stderr)

    if args.fail_on:
        threshold = SEVERITY_RANK[args.fail_on]
        if any(SEVERITY_RANK[f.severity] >= threshold for r in results for f in r.findings):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
