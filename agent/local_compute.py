"""Deterministic, side-effect-free calculations for the dialogue agent.

The language model chooses when this capability is useful.  This module owns
the exact operation once selected, so character counts and arithmetic results
do not depend on token-level language generation.
"""

from __future__ import annotations

import ast
from decimal import Decimal, localcontext
from fractions import Fraction
import unicodedata
from typing import Any, Mapping


MAX_TEXT_CHARACTERS = 50_000
MAX_EXPRESSION_CHARACTERS = 1_000
MAX_REPORTED_POSITIONS = 512
MAX_POWER = 100
MAX_INTEGER_BITS = 4096

SUPPORTED_OPERATIONS = frozenset({
    "count_character",
    "count_substring",
    "text_length",
    "calculate",
})
SUPPORTED_NORMALIZATIONS = frozenset({"none", "NFC", "NFD", "NFKC", "NFKD"})


class LocalComputeError(ValueError):
    """A bounded, user-correctable compute request error."""


def _normalization_name(value: Any) -> str:
    raw = str(value or "NFC").strip()
    normalized = "none" if raw.casefold() == "none" else raw.upper()
    if normalized not in SUPPORTED_NORMALIZATIONS:
        raise LocalComputeError("unsupported_normalization")
    return normalized


def _normalize_text(value: Any, normalization: str) -> str:
    text = str(value or "")
    if len(text) > MAX_TEXT_CHARACTERS:
        raise LocalComputeError("text_too_long")
    return text if normalization == "none" else unicodedata.normalize(normalization, text)


def _comparison_text(value: str, *, case_sensitive: bool) -> str:
    return value if case_sensitive else value.casefold()


def _positions(haystack: str, needle: str, *, overlapping: bool) -> list[int]:
    positions: list[int] = []
    start = 0
    step = 1 if overlapping else len(needle)
    while start <= len(haystack) - len(needle):
        found = haystack.find(needle, start)
        if found < 0:
            break
        positions.append(found)
        start = found + max(1, step)
    return positions


def _with_expected(result: dict[str, Any], expected: Any) -> dict[str, Any]:
    if expected is None or expected == "":
        return result
    try:
        parsed_expected = Fraction(str(expected))
    except (TypeError, ValueError, ZeroDivisionError) as error:
        raise LocalComputeError("invalid_expected_count") from error
    if parsed_expected.denominator != 1 or parsed_expected.numerator < 0:
        raise LocalComputeError("invalid_expected_count")
    expected_count = parsed_expected.numerator
    actual = int(result["result"])
    result.update({
        "expected": expected_count,
        "satisfied": actual == expected_count,
        "difference": actual - expected_count,
    })
    return result


def _count_text(args: Mapping[str, Any], *, character: bool) -> dict[str, Any]:
    normalization = _normalization_name(args.get("normalization"))
    text = _normalize_text(args.get("text"), normalization)
    target = _normalize_text(args.get("target"), normalization)
    if not target:
        raise LocalComputeError("target_required")
    if character and len(target) != 1:
        raise LocalComputeError("target_must_be_one_character")
    case_sensitive = bool(args.get("caseSensitive", args.get("case_sensitive", False)))
    overlapping = bool(args.get("overlapping", False)) and not character
    compared_text = _comparison_text(text, case_sensitive=case_sensitive)
    compared_target = _comparison_text(target, case_sensitive=case_sensitive)
    positions = _positions(compared_text, compared_target, overlapping=overlapping)
    result: dict[str, Any] = {
        "result": len(positions),
        "count": len(positions),
        "target": target,
        "caseSensitive": case_sensitive,
        "normalization": normalization,
        "positions": positions[:MAX_REPORTED_POSITIONS],
        "positionsTruncated": len(positions) > MAX_REPORTED_POSITIONS,
    }
    if not character:
        result["overlapping"] = overlapping
    return _with_expected(result, args.get("expected"))


def _text_length(args: Mapping[str, Any]) -> dict[str, Any]:
    normalization = _normalization_name(args.get("normalization"))
    text = _normalize_text(args.get("text"), normalization)
    unit = str(args.get("unit") or "code_points").strip().lower()
    if unit == "code_points":
        length = len(text)
    elif unit == "non_whitespace":
        length = sum(not character.isspace() for character in text)
    elif unit == "words":
        length = len(text.split())
    elif unit == "lines":
        length = len(text.splitlines()) if text else 0
    else:
        raise LocalComputeError("unsupported_length_unit")
    return _with_expected({
        "result": length,
        "length": length,
        "unit": unit,
        "normalization": normalization,
    }, args.get("expected"))


_ALLOWED_BINARY_OPERATORS = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.FloorDiv: lambda left, right: Fraction(left // right, 1),
    ast.Mod: lambda left, right: left % right,
}
_ALLOWED_UNARY_OPERATORS = {
    ast.UAdd: lambda value: value,
    ast.USub: lambda value: -value,
}


def _bounded_fraction(value: Fraction) -> Fraction:
    if (
        value.numerator.bit_length() > MAX_INTEGER_BITS
        or value.denominator.bit_length() > MAX_INTEGER_BITS
    ):
        raise LocalComputeError("arithmetic_result_too_large")
    return value


def _numeric_literal(node: ast.Constant) -> Fraction:
    value = node.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalComputeError("unsupported_expression")
    try:
        return _bounded_fraction(Fraction(str(value)))
    except (ValueError, ZeroDivisionError) as error:
        raise LocalComputeError("invalid_number") from error


def _evaluate_expression(node: ast.AST) -> Fraction:
    if isinstance(node, ast.Expression):
        return _evaluate_expression(node.body)
    if isinstance(node, ast.Constant):
        return _numeric_literal(node)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY_OPERATORS:
        return _bounded_fraction(
            _ALLOWED_UNARY_OPERATORS[type(node.op)](_evaluate_expression(node.operand))
        )
    if isinstance(node, ast.BinOp):
        left = _evaluate_expression(node.left)
        right = _evaluate_expression(node.right)
        if isinstance(node.op, ast.Pow):
            if right.denominator != 1 or abs(right.numerator) > MAX_POWER:
                raise LocalComputeError("unsupported_power")
            try:
                return _bounded_fraction(left ** right.numerator)
            except ZeroDivisionError as error:
                raise LocalComputeError("division_by_zero") from error
        operator = _ALLOWED_BINARY_OPERATORS.get(type(node.op))
        if operator is None:
            raise LocalComputeError("unsupported_expression")
        try:
            return _bounded_fraction(operator(left, right))
        except ZeroDivisionError as error:
            raise LocalComputeError("division_by_zero") from error
    raise LocalComputeError("unsupported_expression")


def _terminating_decimal(value: Fraction) -> str | None:
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return None
    scale = max(twos, fives)
    numerator = value.numerator * (5 ** (scale - twos)) * (2 ** (scale - fives))
    sign = "-" if numerator < 0 else ""
    digits = str(abs(numerator)).rjust(scale + 1, "0")
    if scale == 0:
        return sign + digits
    whole = digits[:-scale] or "0"
    fraction = digits[-scale:].rstrip("0")
    return sign + whole if not fraction else f"{sign}{whole}.{fraction}"


def _calculate(args: Mapping[str, Any]) -> dict[str, Any]:
    expression = str(args.get("expression") or "").strip()
    if not expression:
        raise LocalComputeError("expression_required")
    if len(expression) > MAX_EXPRESSION_CHARACTERS:
        raise LocalComputeError("expression_too_long")
    try:
        parsed = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as error:
        raise LocalComputeError("invalid_expression") from error
    value = _evaluate_expression(parsed)
    decimal = _terminating_decimal(value)
    exact = decimal if decimal is not None else f"{value.numerator}/{value.denominator}"
    result: dict[str, Any] = {
        "result": exact,
        "exact": True,
        "numerator": value.numerator,
        "denominator": value.denominator,
    }
    if decimal is None:
        with localcontext() as context:
            context.prec = 16
            approximation = Decimal(value.numerator) / Decimal(value.denominator)
        result["decimalApproximation"] = format(approximation, ".15g")
    return result


def execute_local_compute(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """Execute one whitelisted deterministic operation and return a tool result."""

    request = args if isinstance(args, Mapping) else {}
    operation = str(request.get("operation") or "").strip().lower()
    try:
        if operation not in SUPPORTED_OPERATIONS:
            raise LocalComputeError("unsupported_operation")
        if operation == "count_character":
            result = _count_text(request, character=True)
        elif operation == "count_substring":
            result = _count_text(request, character=False)
        elif operation == "text_length":
            result = _text_length(request)
        else:
            result = _calculate(request)
        return {
            "name": "local_compute",
            "ok": True,
            "operation": operation,
            **result,
        }
    except LocalComputeError as error:
        return {
            "name": "local_compute",
            "ok": False,
            "operation": operation,
            "error": str(error),
        }
