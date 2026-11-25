"""Computed channels: values derived from other channels of the same device.

Config (under a device's ``channels:``)::

    cooling_water_dT: {expr: "cooling_water_out - cooling_water_in", unit: K}

Expressions may use channel names, numbers, ``+ - * /``, parentheses and
``abs() min() max()``. Nothing else is evaluated (no Python ``eval``).
"""

from __future__ import annotations

import ast
import operator
from datetime import datetime
from typing import Callable

from labwatch.models import Reading

_BINOPS: dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_FUNCS: dict[str, Callable[..., float]] = {"abs": abs, "min": min, "max": max}


class ExpressionError(ValueError):
    pass


def parse_expr(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"invalid expression {expr!r}: {e.msg}") from None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Name, ast.Load, ast.operator, ast.unaryop)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            continue
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _FUNCS and not node.keywords):
            continue
        raise ExpressionError(f"unsupported syntax in expression {expr!r}: {type(node).__name__}")
    return tree


def expr_names(expr: str) -> set[str]:
    tree = parse_expr(expr)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in _FUNCS}


def evaluate(expr: str, values: dict[str, float]) -> float:
    tree = parse_expr(expr)

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp):
            return _BINOPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp):
            x = ev(node.operand)
            return -x if isinstance(node.op, ast.USub) else x
        if isinstance(node, ast.Call):
            return float(_FUNCS[node.func.id](*(ev(a) for a in node.args)))
        raise ExpressionError(f"unsupported node {type(node).__name__}")

    return ev(tree)


def compute_reading(device: str, spec, readings: dict[str, Reading]) -> Reading:
    """Evaluate a computed channel from the latest readings (invalid if any input is)."""
    names = expr_names(spec.expr)
    inputs = [readings.get(n) for n in names]
    if any(r is None or r.value is None for r in inputs):
        missing = sorted(n for n, r in zip(names, inputs) if r is None or r.value is None)
        return Reading(device, spec.name, f"= {spec.expr}", None, None, spec.unit,
                       status=f"INVALID (no {', '.join(missing)})")
    stamps = [r.timestamp for r in inputs if r.timestamp is not None]
    ts: datetime | None = min(stamps) if stamps else None  # as old as the oldest input
    try:
        value = evaluate(spec.expr, {n: r.value for n, r in zip(names, inputs)}) * spec.scale
    except (ZeroDivisionError, OverflowError):
        return Reading(device, spec.name, f"= {spec.expr}", None, ts, spec.unit, status="INVALID")
    return Reading(device, spec.name, f"= {spec.expr}", value, ts, spec.unit, raw=str(value),
                   status="COMPUTED", label=spec.label_for(value))
