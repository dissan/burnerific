"""Just enough dense linear algebra for small symmetric positive definite systems (n <= 6)."""

from __future__ import annotations

import math

Matrix = list[list[float]]
Vector = list[float]


def zeros(n: int, m: int | None = None) -> Matrix:
    return [[0.0] * (n if m is None else m) for _ in range(n)]


def diag(values: Vector) -> Matrix:
    out = zeros(len(values))
    for i, v in enumerate(values):
        out[i][i] = v
    return out


def cholesky(a: Matrix) -> Matrix:
    """Lower triangular L with L Lᵀ = a. Adds a tiny jitter if a is only semi-definite."""
    n = len(a)
    for jitter in (0.0, 1e-10, 1e-8, 1e-6):
        lo = zeros(n)
        try:
            for i in range(n):
                for j in range(i + 1):
                    s = a[i][j] - sum(lo[i][k] * lo[j][k] for k in range(j))
                    if i == j:
                        s += jitter
                        if s <= 0:
                            raise ValueError
                        lo[i][i] = math.sqrt(s)
                    else:
                        lo[i][j] = s / lo[j][j]
            return lo
        except ValueError:
            continue
    raise ValueError("matrix is not positive definite")


def solve_cholesky(lo: Matrix, b: Vector) -> Vector:
    n = len(lo)
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(lo[i][k] * y[k] for k in range(i))) / lo[i][i]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (y[i] - sum(lo[k][i] * x[k] for k in range(i + 1, n))) / lo[i][i]
    return x


def inverse_spd(a: Matrix) -> Matrix:
    lo = cholesky(a)
    n = len(a)
    cols = [solve_cholesky(lo, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def logdet_spd(a: Matrix) -> float:
    return 2.0 * sum(math.log(row[i]) for i, row in enumerate(cholesky(a)))


def matvec(a: Matrix, x: Vector) -> Vector:
    return [sum(r[j] * x[j] for j in range(len(x))) for r in a]


def dot(x: Vector, y: Vector) -> float:
    return sum(a * b for a, b in zip(x, y, strict=True))


def quad(x: Vector, a: Matrix) -> float:
    """xᵀ a x"""
    return dot(x, matvec(a, x))


def weighted_normal_equations(rows: list[Vector], ys: Vector, ws: Vector) -> tuple[Matrix, Vector, float]:
    """Return (XᵀWX, XᵀWy, yᵀWy)."""
    p = len(rows[0]) if rows else 0
    xtx = zeros(p)
    xty = [0.0] * p
    yty = 0.0
    for x, y, w in zip(rows, ys, ws, strict=True):
        for i in range(p):
            wx = w * x[i]
            xty[i] += wx * y
            for j in range(i + 1):
                xtx[i][j] += wx * x[j]
        yty += w * y * y
    for i in range(p):
        for j in range(i):
            xtx[j][i] = xtx[i][j]
    return xtx, xty, yty
