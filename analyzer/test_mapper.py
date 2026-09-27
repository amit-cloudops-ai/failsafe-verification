"""
FAILSAFE test mapper — factual AST extraction from the test suite.

Produces analyzer/test_coverage_map.json with one entry per test function:
  {
    "name":               fully qualified test name (class.method or plain function name)
    "file":               relative path to the test file
    "line":               source line number
    "endpoints":          HTTP paths exercised (from string literals like "/payment")
    "functions":          app functions called directly (from imports + call sites)
    "covers": {
      "happy_path":         bool — payment succeeded end-to-end (201)
      "business_error_path":bool — expected business failure (e.g. insufficient funds / 402)
      "validation":         bool — input schema / constraint rejection (400 / 422)
      "retry":              bool
      "concurrency":        bool
      "partial_failure":    bool
    }
  }

The six scenario tags are INDEPENDENT BOOLEANS, not mutually exclusive.
A test may carry multiple True values (e.g. a validation test is also happy_path
from the system's perspective if the rejection is the intended outcome).
"""

import ast
import json
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TEST_FILE = Path(__file__).parent.parent / "tests" / "test_payment.py"
OUTPUT = Path(__file__).parent / "test_coverage_map.json"

# Keywords whose presence (in name or string literals in the body) signals a scenario.
SCENARIO_KEYWORDS: dict[str, list[str]] = {
    "retry": [
        "retry", "retries", "idempotent", "idempotency",
        "duplicate", "twice", "again", "resend", "replay",
    ],
    "concurrency": [
        "concurrent", "concurrency", "thread", "parallel",
        "race", "race_condition", "simultaneous", "async",
    ],
    "partial_failure": [
        "partial", "rollback", "compensat", "timeout",
        "partial_failure", "half", "crash", "interrupt",
    ],
}

# Class-name prefixes / fragments that hint at the scenario category.
CLASS_HINTS: dict[str, list[str]] = {
    "happy_path":          ["successful", "success", "happy"],
    "business_error_path": ["insufficientfunds", "insufficient_funds", "declined",
                            "businesserror", "business_error", "fundserror"],
    "validation":          ["validation", "invalid", "reject", "badrequest", "bad_request"],
    "retry":               ["retry", "duplicate", "idempoten"],
    "concurrency":         ["concurren", "thread", "race", "parallel"],
    "partial_failure":     ["partial", "rollback", "failure", "crash"],
}

# Status-code patterns that imply validation failure (422 / 400).
VALIDATION_STATUS = {400, 422}

# Endpoint path regex
ENDPOINT_RE = re.compile(r'"(/[a-zA-Z0-9_/{}?&=-]*)"')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _string_literals(body: list[ast.stmt]) -> list[str]:
    """Collect every string constant from a list of AST statements."""
    result: list[str] = []
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            result.append(node.value)
    return result


def _call_names(body: list[ast.stmt]) -> list[str]:
    """Collect every unique call name (qualified) from a list of statements."""
    names: set[str] = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name):
                    names.add(f"{func.value.id}.{func.attr}")
                else:
                    names.add(func.attr)
    return sorted(names)


def _status_codes_in_body(body: list[ast.stmt]) -> set[int]:
    """Extract integer literals that look like HTTP status codes (3-digit >= 100)."""
    codes: set[int] = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            if 100 <= node.value < 600:
                codes.add(node.value)
    return codes


def _endpoints_in_body(body: list[ast.stmt]) -> list[str]:
    """Extract URL path strings like '/payment' from the test body."""
    paths: set[str] = set()
    for s in _string_literals(body):
        if s.startswith("/"):
            paths.add(s)
    return sorted(paths)


def _detect_scenarios(
    test_name: str,
    class_name: str | None,
    body_strings: list[str],
    body_int_codes: set[int],
) -> dict[str, bool]:
    """
    Classify a test across six independent boolean scenario tags.

    happy_path:
        The test verifies that a payment succeeded end-to-end (201).
        Signal: class hint (Successful/Success), or name contains '201' /
        'returns_201' / 'accepted' / 'completed' / 'decremented' / 'prefixed' /
        'preserved' / 'default' / 'exact_balance'.
        Does NOT fire purely because no other tag fires — that catch-all was
        the root cause of the misclassification.

    business_error_path:
        The test verifies an expected business-level failure — a payment the
        system intentionally declined (e.g. insufficient funds → 402).
        Signal: class hint (InsufficientFunds / Declined), or name contains
        'funds' / 'declined' / 'insufficient' / 'returns_402' /
        'balance_unchanged' / 'error_detail' / 'zero_balance', or body
        asserts status 402.

    validation:
        The test verifies that malformed / out-of-range input is rejected
        before business logic runs (400 / 422).
        Signal: class hint (Validation / Invalid), or name contains 'invalid' /
        'reject' / 'missing' / 'negative' / 'bad_request', or body asserts
        status 422 or 400.

    retry / concurrency / partial_failure:
        Keyword-driven — see SCENARIO_KEYWORDS.
    """
    name_lower = test_name.lower()
    class_lower = (class_name or "").lower()
    all_text = " ".join([name_lower, class_lower] + [s.lower() for s in body_strings])

    def _any_kw(kw_list: list[str]) -> bool:
        return any(kw in all_text for kw in kw_list)

    def _class_hint(scenario: str) -> bool:
        return any(h in class_lower for h in CLASS_HINTS.get(scenario, []))

    retry = _any_kw(SCENARIO_KEYWORDS["retry"])
    concurrency = _any_kw(SCENARIO_KEYWORDS["concurrency"])
    partial_failure = _any_kw(SCENARIO_KEYWORDS["partial_failure"])

    # validation: input schema/constraint rejection — 400/422 only, never 402.
    validation = (
        _class_hint("validation")
        or any(kw in name_lower for kw in ["invalid", "reject", "missing", "negative", "bad_request"])
        or 422 in body_int_codes
        or 400 in body_int_codes
    )

    # business_error_path: expected business-level decline — 402 / insufficient funds.
    business_error_path = (
        _class_hint("business_error_path")
        or any(kw in name_lower for kw in [
            "funds", "declined", "insufficient", "returns_402",
            "balance_unchanged", "error_detail", "zero_balance",
        ])
        or 402 in body_int_codes
    )

    # happy_path: payment succeeded end-to-end (201).
    # Explicit signals only — no catch-all fallback, which previously caused
    # business_error_path tests to be misclassified as happy_path.
    happy_path = (
        _class_hint("happy_path")
        or any(kw in name_lower for kw in [
            "returns_201", "accepted", "completed", "decremented",
            "prefixed", "preserved", "default", "exact_balance",
        ])
        or 201 in body_int_codes
    )

    return {
        "happy_path": happy_path,
        "business_error_path": business_error_path,
        "validation": validation,
        "retry": retry,
        "concurrency": concurrency,
        "partial_failure": partial_failure,
    }


# ---------------------------------------------------------------------------
# AST traversal
# ---------------------------------------------------------------------------

def _collect_tests(tree: ast.Module, rel_path: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    # Top-level test functions
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            body_strings = _string_literals(node.body)
            body_codes = _status_codes_in_body(node.body)
            results.append({
                "name": node.name,
                "class": None,
                "file": rel_path,
                "line": node.lineno,
                "endpoints": _endpoints_in_body(node.body),
                "functions": _call_names(node.body),
                "covers": _detect_scenarios(node.name, None, body_strings, body_codes),
            })

        # Test classes
        if isinstance(node, ast.ClassDef):
            class_name = node.name
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"):
                    body_strings = _string_literals(item.body)
                    body_codes = _status_codes_in_body(item.body)
                    results.append({
                        "name": f"{class_name}.{item.name}",
                        "class": class_name,
                        "file": rel_path,
                        "line": item.lineno,
                        "endpoints": _endpoints_in_body(item.body),
                        "functions": _call_names(item.body),
                        "covers": _detect_scenarios(item.name, class_name, body_strings, body_codes),
                    })

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    source = TEST_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(TEST_FILE))

    rel_path = str(TEST_FILE.relative_to(TEST_FILE.parent.parent))
    tests = _collect_tests(tree, rel_path)

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(tests, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    print(f"  {len(tests)} test functions mapped")

    # Summary counts
    for scenario in ("happy_path", "business_error_path", "validation", "retry", "concurrency", "partial_failure"):
        count = sum(1 for t in tests if t["covers"][scenario])
        print(f"  {scenario}: {count}")


if __name__ == "__main__":
    main()
