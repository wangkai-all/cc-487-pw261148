"""Small, dependency-free statistics helpers used by the analysis engine.

Everything here works on plain Python numbers so the tool runs in an offline
environment with only the standard library.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

Z_975 = 1.959963984540054


# ---------------------------------------------------------------------------
# Normal distribution
# ---------------------------------------------------------------------------
def normal_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def normal_ppf(p: float) -> float:
    """Inverse standard normal CDF via bisection (accurate to ~1e-12)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    lo, hi = -10.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if normal_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# Incomplete beta / Student t
# ---------------------------------------------------------------------------
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz)."""
    tiny = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-14:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, df: float) -> float:
    if df <= 0:
        raise ValueError("df must be positive")
    x = df / (df + t * t)
    tail = 0.5 * regularized_incomplete_beta(df / 2.0, 0.5, x)
    return 1.0 - tail if t >= 0 else tail


def student_t_ppf(p: float, df: float) -> float:
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    lo, hi = -200.0, 200.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if student_t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# Chi-square / gamma
# ---------------------------------------------------------------------------
def _gammaincc(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x)."""
    if x <= 0.0:
        return 1.0
    if x < a + 1.0:
        # Series for P(a, x), then complement.
        term = 1.0 / a
        total = term
        ap = a
        for _ in range(1000):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        p = total * math.exp(-x + a * math.log(x) - math.lgamma(a))
        return max(0.0, min(1.0, 1.0 - p))
    # Continued fraction for Q(a, x), modified Lentz.
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    q = math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
    return max(0.0, min(1.0, q))


def chi_square_sf(x: float, df: int) -> float:
    if df <= 0:
        raise ValueError("df must be positive")
    if x <= 0:
        return 1.0
    return _gammaincc(df / 2.0, x / 2.0)


def chi_square_test(observed: Sequence[float], expected: Sequence[float]) -> dict:
    stat = 0.0
    for obs, exp in zip(observed, expected):
        if exp > 0:
            stat += (obs - exp) ** 2 / exp
    df = max(1, len(observed) - 1)
    return {"statistic": stat, "df": df, "p_value": chi_square_sf(stat, df)}


# ---------------------------------------------------------------------------
# Effect estimation
# ---------------------------------------------------------------------------
def _safe_ratio(num: float, den: float):
    return None if den == 0 else num / den


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def sample_sd(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def compare_proportions(x_control: int, n_control: int, x_treat: int, n_treat: int) -> dict:
    """Two-sided two-proportion z-test, effect expressed as treatment - control."""
    p_c = x_control / n_control if n_control else None
    p_t = x_treat / n_treat if n_treat else None
    out = {
        "control": {"n": n_control, "successes": x_control, "value": p_c},
        "treatment": {"n": n_treat, "successes": x_treat, "value": p_t},
        "effect": {
            "diff": None,
            "ci_low": None,
            "ci_high": None,
            "p_value": None,
            "relative": None,
            "significant": False,
        },
    }
    if not n_control or not n_treat:
        return out
    diff = p_t - p_c
    se = math.sqrt(p_c * (1 - p_c) / n_control + p_t * (1 - p_t) / n_treat)
    pooled = (x_control + x_treat) / (n_control + n_treat)
    se_null = math.sqrt(pooled * (1 - pooled) * (1 / n_control + 1 / n_treat))
    if se_null > 0:
        z = diff / se_null
        p_value = 2.0 * (1.0 - normal_cdf(abs(z)))
    else:
        z = 0.0
        p_value = 1.0
    effect = out["effect"]
    effect["diff"] = diff
    effect["z"] = z
    effect["p_value"] = p_value
    effect["relative"] = _safe_ratio(diff, p_c)
    effect["significant"] = p_value < 0.05
    if se > 0:
        effect["ci_low"] = diff - Z_975 * se
        effect["ci_high"] = diff + Z_975 * se
    else:
        effect["ci_low"] = effect["ci_high"] = diff
    return out


def compare_continuous(control: Iterable[float], treatment: Iterable[float]) -> dict:
    c = list(control)
    t = list(treatment)
    n_c, n_t = len(c), len(t)
    out = {
        "control": {"n": n_c, "value": mean(c) if n_c else None, "sd": sample_sd(c) if n_c else None},
        "treatment": {"n": n_t, "value": mean(t) if n_t else None, "sd": sample_sd(t) if n_t else None},
        "effect": {
            "diff": None,
            "ci_low": None,
            "ci_high": None,
            "p_value": None,
            "relative": None,
            "significant": False,
        },
    }
    if n_c < 2 or n_t < 2:
        return out
    m_c, m_t = out["control"]["value"], out["treatment"]["value"]
    s_c, s_t = out["control"]["sd"], out["treatment"]["sd"]
    se = math.sqrt(s_c * s_c / n_c + s_t * s_t / n_t)
    diff = m_t - m_c
    effect = out["effect"]
    effect["diff"] = diff
    effect["relative"] = _safe_ratio(diff, m_c) if m_c else None
    if se == 0:
        effect["p_value"] = 1.0
        effect["ci_low"] = effect["ci_high"] = diff
        return out
    df_num = (s_c * s_c / n_c + s_t * s_t / n_t) ** 2
    df_den = 0.0
    if n_c > 1:
        df_den += (s_c * s_c / n_c) ** 2 / (n_c - 1)
    if n_t > 1:
        df_den += (s_t * s_t / n_t) ** 2 / (n_t - 1)
    df = df_num / df_den if df_den > 0 else n_c + n_t - 2
    t_stat = diff / se
    p_value = 2.0 * (1.0 - student_t_cdf(abs(t_stat), df))
    crit = student_t_ppf(0.975, df)
    effect["p_value"] = p_value
    effect["t"] = t_stat
    effect["df"] = df
    effect["ci_low"] = diff - crit * se
    effect["ci_high"] = diff + crit * se
    effect["significant"] = p_value < 0.05
    return out


def standardized_mean_diff(control: Sequence[float], treatment: Sequence[float]) -> float | None:
    if len(control) < 2 or len(treatment) < 2:
        return None
    var_c = sample_sd(control) ** 2
    var_t = sample_sd(treatment) ** 2
    pooled = math.sqrt((var_c + var_t) / 2.0)
    if pooled == 0:
        return 0.0
    return (mean(treatment) - mean(control)) / pooled


def wilson_interval(successes: int, n: int, z: float = Z_975) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))
