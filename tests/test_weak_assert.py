"""Tests for weak_assert.py — stdlib unittest, no third-party dependencies."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import weak_assert as wa  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.dir)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def scan(self, source, filename="test_sample.py"):
        path = self.dir / filename
        path.write_text(source, encoding="utf-8")
        result = wa.analyze_python(path, filename)
        result.findings = wa.dedupe(result.findings)
        return result

    def rules(self, source, filename="test_sample.py"):
        return [f.rule for f in self.scan(source, filename).findings]

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wa.main(argv)
        return code, out.getvalue(), err.getvalue()


class TestWA001NoAssertion(Base):
    def test_flags_test_without_assertion(self):
        src = "def test_add():\n    add(1, 2)\n"
        self.assertIn("WA001", self.rules(src))

    def test_does_not_flag_real_assertion(self):
        src = "def test_add():\n    assert add(1, 2) == 3\n"
        self.assertNotIn("WA001", self.rules(src))

    def test_mock_assertion_counts(self):
        src = (
            "def test_call(mocker):\n"
            "    mocker.patch('x.y')\n"
            "    do_work()\n"
            "    mocker.assert_called_once_with()\n"
        )
        self.assertNotIn("WA001", self.rules(src))

    def test_pytest_raises_counts(self):
        src = (
            "import pytest\n"
            "def test_raises():\n"
            "    with pytest.raises(ValueError):\n"
            "        parse('')\n"
        )
        self.assertNotIn("WA001", self.rules(src))

    def test_unittest_assert_raises_counts(self):
        src = (
            "class TestX(unittest.TestCase):\n"
            "    def test_raises(self):\n"
            "        self.assertRaises(ValueError, parse, '')\n"
        )
        self.assertNotIn("WA001", self.rules(src))

    def test_helper_function_is_not_a_test(self):
        src = "def helper():\n    add(1, 2)\n"
        self.assertEqual(self.rules(src), [])

    def test_nested_test_helper_is_not_counted(self):
        src = (
            "def test_outer():\n"
            "    def test_inner():\n"
            "        add(1, 2)\n"
            "    assert add(1, 2) == 3\n"
        )
        result = self.scan(src)
        self.assertEqual(result.tests, 1)
        self.assertNotIn("WA001", [f.rule for f in result.findings])


class TestWA002Constants(Base):
    def test_assert_true(self):
        self.assertIn("WA002", self.rules("def test_x():\n    assert True\n"))

    def test_assert_literal_comparison(self):
        self.assertIn("WA002", self.rules("def test_x():\n    assert 1 == 1\n"))

    def test_assert_equal_literals(self):
        self.assertIn("WA002", self.rules(
            "def test_x():\n    self.assertEqual(1, 1)\n"))

    def test_real_comparison_not_flagged(self):
        src = "def test_x():\n    assert compute() == 3\n"
        self.assertNotIn("WA002", self.rules(src))


class TestWA003Truthiness(Base):
    def test_bare_name(self):
        self.assertIn("WA003", self.rules("def test_x():\n    assert result\n"))

    def test_bare_attribute(self):
        self.assertIn("WA003", self.rules("def test_x():\n    assert obj.ready\n"))

    def test_negation(self):
        self.assertIn("WA003", self.rules("def test_x():\n    assert not items\n"))

    def test_assert_true_name_not_flagged(self):
        # assertTrue(bool_result) is explicit about the boolean; flagging it
        # produced too much noise on real suites.
        src = "def test_x():\n    self.assertTrue(result)\n"
        self.assertNotIn("WA003", self.rules(src))

    def test_is_not_none_is_fine(self):
        src = "def test_x():\n    assert result is not None\n"
        self.assertNotIn("WA003", self.rules(src))

    def test_isinstance_is_fine(self):
        src = "def test_x():\n    assert isinstance(result, str)\n"
        self.assertNotIn("WA003", self.rules(src))

    def test_comparison_is_fine(self):
        self.assertNotIn("WA003", self.rules("def test_x():\n    assert len(x) > 0\n"))


class TestWA004BroadException(Base):
    def test_pytest_raises_exception(self):
        src = (
            "import pytest\n"
            "def test_x():\n"
            "    with pytest.raises(Exception):\n"
            "        parse('')\n"
        )
        self.assertIn("WA004", self.rules(src))

    def test_assert_raises_base_exception(self):
        src = (
            "def test_x():\n"
            "    self.assertRaises(BaseException, parse, '')\n"
        )
        self.assertIn("WA004", self.rules(src))

    def test_specific_exception_is_fine(self):
        src = (
            "import pytest\n"
            "def test_x():\n"
            "    with pytest.raises(ValueError):\n"
            "        parse('')\n"
        )
        self.assertNotIn("WA004", self.rules(src))

    def test_only_one_finding_per_site(self):
        src = (
            "import pytest\n"
            "def test_x():\n"
            "    with pytest.raises(Exception):\n"
            "        parse('')\n"
        )
        self.assertEqual(self.rules(src).count("WA004"), 1)


class TestWA005Swallowed(Base):
    def test_swallowed_by_pass(self):
        src = (
            "def test_x():\n"
            "    try:\n"
            "        assert add(1, 2) == 4\n"
            "    except AssertionError:\n"
            "        pass\n"
        )
        self.assertIn("WA005", self.rules(src))

    def test_reraise_is_fine(self):
        src = (
            "def test_x():\n"
            "    try:\n"
            "        assert add(1, 2) == 4\n"
            "    except AssertionError:\n"
            "        raise\n"
        )
        self.assertNotIn("WA005", self.rules(src))

    def test_handler_that_asserts_is_fine(self):
        src = (
            "def test_x():\n"
            "    try:\n"
            "        assert add(1, 2) == 4\n"
            "    except AssertionError as exc:\n"
            "        assert '4' in str(exc)\n"
        )
        self.assertNotIn("WA005", self.rules(src))


class TestWA006SelfComparison(Base):
    def test_compare_self(self):
        self.assertIn("WA006", self.rules("def test_x():\n    assert value == value\n"))

    def test_assert_equal_self(self):
        self.assertIn("WA006", self.rules(
            "def test_x():\n    self.assertEqual(value, value)\n"))

    def test_different_values_not_flagged(self):
        self.assertNotIn("WA006", self.rules("def test_x():\n    assert a == b\n"))


class TestWA007DeadAssertion(Base):
    def test_assert_after_return(self):
        src = (
            "def test_x():\n"
            "    assert a == b\n"
            "    return\n"
            "    assert c == d\n"
        )
        self.assertIn("WA007", self.rules(src))

    def test_conditional_return_not_flagged(self):
        src = (
            "def test_x():\n"
            "    if fast:\n"
            "        return\n"
            "    assert c == d\n"
        )
        self.assertNotIn("WA007", self.rules(src))


class TestWA008Skips(Base):
    def test_bare_pytest_skip(self):
        src = "import pytest\n@pytest.mark.skip\ndef test_x():\n    assert 1 == 1\n"
        self.assertIn("WA008", self.rules(src))

    def test_skip_with_reason_is_fine(self):
        src = (
            "import pytest\n"
            "@pytest.mark.skip(reason='waiting on upstream fix')\n"
            "def test_x():\n"
            "    assert 1 == 1\n"
        )
        self.assertNotIn("WA008", self.rules(src))

    def test_skipif_with_variable_reason_is_fine(self):
        src = (
            "import pytest\n"
            "REASON = 'not on windows'\n"
            "@pytest.mark.skipif(sys.platform == 'win32', reason=REASON)\n"
            "def test_x():\n"
            "    assert 1 == 1\n"
        )
        self.assertNotIn("WA008", self.rules(src))

    def test_unittest_skip_with_reason_is_fine(self):
        src = (
            "@unittest.skip('needs a database')\n"
            "def test_x():\n"
            "    assert 1 == 1\n"
        )
        self.assertNotIn("WA008", self.rules(src))


class TestSuppressionAndCli(Base):
    def test_inline_suppression(self):
        src = "def test_x():\n    assert result  # weak-assert: ignore=WA003\n"
        self.assertNotIn("WA003", self.rules(src))

    def test_inline_suppression_all_rules(self):
        src = "def test_x():\n    assert result  # weak-assert: ignore\n"
        self.assertEqual(self.rules(src), [])

    def test_file_ignore(self):
        src = "# weak-assert: ignore-file\ndef test_x():\n    add(1)\n"
        self.assertEqual(self.rules(src), [])

    def test_cli_ignore(self):
        (self.dir / "test_sample.py").write_text(
            "def test_x():\n    assert result  # weak-assert: ignore=WA003\n", encoding="utf-8")
        code, out, _ = self.run_cli(["test_sample.py", "--ignore", "WA003"])
        self.assertEqual(code, 0)
        self.assertNotIn("WA003", out)

    def test_cli_fail_on_high(self):
        (self.dir / "test_sample.py").write_text(
            "def test_x():\n    assert x == x\n", encoding="utf-8")
        code, _, _ = self.run_cli(["test_sample.py", "--fail-on", "high"])
        self.assertEqual(code, 1)

    def test_cli_fail_on_below_threshold(self):
        (self.dir / "test_sample.py").write_text(
            "def test_x():\n    assert result\n", encoding="utf-8")
        code, _, _ = self.run_cli(["test_sample.py", "--fail-on", "high"])
        self.assertEqual(code, 0)

    def test_cli_reports(self):
        (self.dir / "test_sample.py").write_text(
            "def test_x():\n    add(1)\n", encoding="utf-8")
        code, _, _ = self.run_cli([
            "test_sample.py", "--json", "out.json", "--md", "out.md", "--quiet",
        ])
        self.assertEqual(code, 0)
        self.assertTrue((self.dir / "out.json").exists())
        self.assertTrue((self.dir / "out.md").exists())
        data = __import__("json").loads((self.dir / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["tests_scanned"], 1)
        self.assertEqual(data["findings"][0]["rule"], "WA001")

    def test_missing_path_exit_2(self):
        code, _, _ = self.run_cli(["does-not-exist"])
        self.assertEqual(code, 2)

    def test_syntax_error_is_skipped_not_crashed(self):
        result = self.scan("def broken(:\n    pass\n")
        self.assertFalse(result.parsed)
        self.assertEqual(result.findings, [])

    def test_class_method_qualname(self):
        src = (
            "class TestThing(unittest.TestCase):\n"
            "    def test_method(self):\n"
            "        assert 1 == 2\n"
        )
        result = self.scan(src)
        self.assertEqual(result.findings[0].symbol, "TestThing.test_method")

    def test_summary_counts(self):
        src = (
            "def test_a():\n    assert 1 == 1\n"
            "def test_b():\n    add(1)\n"
        )
        result = self.scan(src)
        summary = wa.summarize([result])
        self.assertEqual(summary["tests_scanned"], 2)
        self.assertEqual(summary["assertion_free_tests"], 1)


class TestRegressionFixes(Base):
    """Cases found by adversarial verification."""

    def test_skip_call_is_not_assertion_free(self):
        src = (
            "import pytest\n"
            "def test_a():\n    pytest.skip('no db')\n"
            "def test_b():\n    raise unittest.SkipTest('no db')\n"
            "def test_c(self):\n    self.skipTest('no db')\n"
        )
        result = self.scan(src)
        self.assertNotIn("WA001", [f.rule for f in result.findings])
        self.assertEqual(result.tests_skipped, 3)

    def test_compound_comparison_not_truthiness(self):
        src = "def test_a():\n    assert x == 1 and y == 2\n"
        self.assertNotIn("WA003", self.rules(src))

    def test_negated_isinstance_not_truthiness(self):
        src = "def test_a():\n    assert not isinstance(x, str)\n"
        self.assertNotIn("WA003", self.rules(src))

    def test_handler_that_cannot_catch_assertion(self):
        src = (
            "def test_a():\n"
            "    try:\n"
            "        assert add(1, 2) == 4\n"
            "    except ValueError:\n"
            "        pass\n"
        )
        self.assertNotIn("WA005", self.rules(src))

    def test_handler_asserting_with_unittest_method(self):
        src = (
            "def test_a():\n"
            "    try:\n"
            "        assert add(1, 2) == 4\n"
            "    except AssertionError as exc:\n"
            "        self.assertEqual(str(exc), '1 + 2')\n"
        )
        self.assertNotIn("WA005", self.rules(src))

    def test_conditional_raise_in_handler_is_not_swallowing(self):
        src = (
            "def test_a():\n"
            "    try:\n"
            "        assert add(1, 2) == 4\n"
            "    except AssertionError:\n"
            "        if cleanup_failed:\n"
            "            raise\n"
        )
        self.assertNotIn("WA005", self.rules(src))

    def test_bom_file_is_scanned(self):
        path = self.dir / "test_bom.py"
        path.write_bytes(b"\xef\xbb\xbfdef test_a():\n    add(1)\n")
        result = wa.analyze_python(path, "test_bom.py")
        self.assertTrue(result.parsed)
        self.assertIn("WA001", [f.rule for f in result.findings])

    def test_nul_byte_is_skipped_not_crashing(self):
        path = self.dir / "test_nul.py"
        path.write_bytes(b"def test_a():\n    assert True  # \x00\n")
        result = wa.analyze_python(path, "test_nul.py")
        self.assertFalse(result.parsed)
        self.assertEqual(result.findings, [])

    def test_unittest_style_testfoo_is_discovered(self):
        src = "def testFoo():\n    add(1)\n"
        self.assertIn("WA001", self.rules(src))

    def test_assert_true_literal_is_constant(self):
        src = "def test_a():\n    self.assertTrue(True)\n    self.assertTrue([])\n"
        self.assertEqual(self.rules(src).count("WA002"), 2)

    def test_unittest_skipif_positional_reason(self):
        src = "@unittest.skipIf(True, 'no db')\ndef test_a():\n    assert 1 == 1\n"
        self.assertNotIn("WA008", self.rules(src))

    def test_ignore_file_after_header(self):
        src = (
            '"""module docstring"""\n'
            "import os\n"
            "import sys\n"
            "\n"
            "CONST = 1\n"
            "MORE = 2\n"
            "# weak-assert: ignore-file\n"
            "def test_a():\n"
            "    add(1)\n"
        )
        self.assertEqual(self.rules(src), [])

    def test_subtest_alone_is_not_an_assertion(self):
        src = (
            "def test_a(self):\n"
            "    with self.subTest(case=1):\n"
            "        add(1)\n"
        )
        self.assertIn("WA001", self.rules(src))

    def test_dead_assertion_after_pass(self):
        src = (
            "def test_a():\n"
            "    assert a == b\n"
            "    return\n"
            "    pass\n"
            "    assert c == d\n"
        )
        self.assertIn("WA007", self.rules(src))

    def test_marker_inside_string_does_not_disable_file(self):
        src = (
            "def test_a():\n"
            '    marker = "# weak-assert: ignore-file"\n'
            "    add(1)\n"
        )
        self.assertIn("WA001", self.rules(src))

    def test_report_path_is_directory_exit_2(self):
        (self.dir / "test_sample.py").write_text("def test_a():\n    add(1)\n", encoding="utf-8")
        (self.dir / "out").mkdir()
        code, _, err = self.run_cli(["test_sample.py", "--json", "out", "--quiet"])
        self.assertEqual(code, 2)
        self.assertIn("cannot write", err)


if __name__ == "__main__":
    unittest.main()
