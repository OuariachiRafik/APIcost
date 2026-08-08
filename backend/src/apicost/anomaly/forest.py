"""Slow-path pattern detection — UC-32, BUILD_SPEC §4 P6.

The z-score in ``zscore.py`` watches one number: spend rate. That catches a
runaway loop, which is loud. It does not catch the case this module exists for
— **a leaked key being used at ordinary volume**. Someone else's traffic on your
key costs about what your traffic costs; what differs is its *shape*. Different
models, different endpoints, prompts that never repeat.

So this scores a feature vector rather than a scalar, using an IsolationForest
fitted on the project's own recent history. Isolation forests are the right tool
here for an unglamorous reason: they need no labels. Nobody can hand us examples
of their key being stolen.

Runs every 5 minutes on the worker, never on the request path.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "FEATURE_NAMES",
    "MIN_HISTORY_WINDOWS",
    "PatternFeatures",
    "PatternVerdict",
    "detect",
    "features_from_rows",
]

FEATURE_NAMES: tuple[str, ...] = (
    "request_rate",
    "cost_rate",
    "model_entropy",
    "endpoint_entropy",
    "unique_prompt_ratio",
)
"""Fixed order, for the same reason the router's feature list is fixed: the
vector is positional, and reordering these would silently compare one feature
against another's distribution."""

MIN_HISTORY_WINDOWS: int = 24
"""Two hours of 5-minute windows. Below this an IsolationForest has not seen
enough of the project's normal shape to call anything abnormal, and would
mostly flag whatever is least like the first few windows."""

CONTAMINATION: float = 0.05
"""Expect ~5% of historical windows to look odd. Setting this lower makes the
detector so reluctant that a slow-building abuse pattern becomes the new normal
before it ever fires."""

SCORE_THRESHOLD: float = -0.15
"""IsolationForest decision_function is negative for outliers. Zero would fire
on every mildly unusual window; this asks for a clear one. Tuned to be quieter
than the z-score path, because this one is inherently fuzzier and a false
"your key may be leaked" is an alarming thing to send someone."""

DEVIATION_FLOOR: float = 3.0
"""How far a feature must move, in robust units, before the forest's opinion
counts.

This is a **necessary** condition, not a second detector, and it exists because
an isolation forest measures uniqueness rather than distance. A well-behaved
project holds several of these features perfectly constant, so the window being
scored is the only point that differs on them at all — and the forest isolates
it in one split whether the move was 0.05 to 0.051 or 0.1 to 2.4. Measured: a
benign window scored -0.34, indistinguishable from a leaked-key window at -0.38.

Same shape of argument as ``MIN_ABSOLUTE_RATE_USD`` in the z-score path:
relative anomaly is necessary but not sufficient."""


@dataclass(frozen=True)
class PatternFeatures:
    """One window's shape. All non-negative, all per minute where they are
    rates, so windows of different lengths stay comparable."""

    request_rate: float
    cost_rate: float
    model_entropy: float
    endpoint_entropy: float
    unique_prompt_ratio: float

    def as_vector(self) -> list[float]:
        return [getattr(self, name) for name in FEATURE_NAMES]


@dataclass(frozen=True)
class PatternVerdict:
    anomalous: bool
    score: float
    reason: str
    features: PatternFeatures | None = None
    contributors: list[str] | None = None
    """Which features sat furthest from the historical median. Not a causal
    explanation — the forest does not produce one — but it is the difference
    between an email that says "unusual model mix and unfamiliar endpoints" and
    one that says "anomaly score -0.31"."""


def _entropy(counts: Sequence[int]) -> float:
    """Shannon entropy in bits, 0.0 for a single category.

    Entropy rather than a distinct count because it is robust to a long tail:
    a project that uses one model for 99.9% of calls and a second one twice has
    a distinct count of 2 and an entropy near 0, and near 0 is the honest
    description of its behaviour.
    """
    total = sum(counts)
    if total <= 0:
        return 0.0
    entropy = 0.0
    for count in counts:
        if count <= 0:
            continue
        p = count / total
        entropy -= p * math.log2(p)
    return entropy


def features_from_rows(
    rows: Sequence[dict[str, Any]],
    *,
    window_minutes: float,
) -> PatternFeatures:
    """Summarise one window of ledger rows.

    Each row needs ``model_used``, ``endpoint``, ``cost_usd``, and
    ``prompt_hash``. ``prompt_hash`` may be ``None`` — projects that have not
    opted into storing raw content still have a hash, but a row predating the
    cache does not, and those are simply not counted toward uniqueness.
    """
    if window_minutes <= 0:
        window_minutes = 1.0

    count = len(rows)
    if count == 0:
        return PatternFeatures(0.0, 0.0, 0.0, 0.0, 0.0)

    models = Counter(str(r.get("model_used") or "unknown") for r in rows)
    endpoints = Counter(str(r.get("endpoint") or "unknown") for r in rows)

    total_cost = 0.0
    for row in rows:
        try:
            total_cost += float(row.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            continue

    hashes = [r.get("prompt_hash") for r in rows if r.get("prompt_hash")]
    # A high ratio means every prompt is different. Human-driven traffic repeats
    # far more than people expect; scripted abuse enumerates.
    unique_ratio = len(set(hashes)) / len(hashes) if hashes else 0.0

    return PatternFeatures(
        request_rate=count / window_minutes,
        cost_rate=total_cost / window_minutes,
        model_entropy=_entropy(list(models.values())),
        endpoint_entropy=_entropy(list(endpoints.values())),
        unique_prompt_ratio=unique_ratio,
    )


def detect(
    history: Sequence[PatternFeatures],
    current: PatternFeatures,
    *,
    min_history: int = MIN_HISTORY_WINDOWS,
    threshold: float = SCORE_THRESHOLD,
) -> PatternVerdict:
    """Score the current window against the project's own history.

    Never raises. A detector that throws takes the alerting path down with it,
    and the alerting path is what the user is relying on when something is
    already wrong.
    """
    if len(history) < min_history:
        return PatternVerdict(False, 0.0, "COLD_START", current)

    try:
        # Imported here, not at module scope: sklearn costs ~1 s to import and
        # this module is reachable from the API process, which should not pay
        # for a worker-only dependency at startup.
        import numpy as np
        from sklearn.ensemble import IsolationForest

        train = np.array([f.as_vector() for f in history], dtype=float)
        point = np.array([current.as_vector()], dtype=float)

        if not np.isfinite(train).all() or not np.isfinite(point).all():
            return PatternVerdict(False, 0.0, "NON_FINITE_FEATURES", current)

        forest = IsolationForest(
            n_estimators=100,
            contamination=CONTAMINATION,
            random_state=0,
            # Deterministic given the same history: two runs over the same data
            # must not disagree about whether the user's key is leaking.
        )

        # Fit on the history **including** the point being scored. This is not
        # a leak, and getting it wrong made the detector blind to the exact
        # scenario UC-32 exists for.
        #
        # A tree can only split a feature between the min and max it saw while
        # fitting. A well-behaved project has *constant* model entropy, endpoint
        # entropy and unique-prompt ratio — that is what being well-behaved
        # means — so a forest fit on history alone has a degenerate range on
        # precisely the three features a leaked key changes. It never splits on
        # them, and the stolen-key window lands in the same leaf as everything
        # else. Measured: 0.014 (normal) fitting on history alone, -0.377
        # (clearly anomalous) fitting on both, for the same vectors.
        #
        # One point among 40+ moves the model negligibly; what it does is make
        # the deviant dimension splittable at all.
        forest.fit(np.vstack([train, point]))
        score = float(forest.decision_function(point)[0])

        if score >= threshold:
            return PatternVerdict(False, score, "WITHIN_NORMAL", current)

        deviation = _deviation(train, current)
        if float(deviation.max()) < DEVIATION_FLOOR:
            # Unique, but not by enough to be worth an email.
            return PatternVerdict(False, score, "WITHIN_NORMAL_MAGNITUDE", current)

        return PatternVerdict(
            True,
            score,
            "UNUSUAL_PATTERN",
            current,
            _rank_contributors(deviation),
        )
    except Exception:
        return PatternVerdict(False, 0.0, "DETECTOR_FAILED", current)


def _deviation(train: Any, current: PatternFeatures) -> Any:
    """Per-feature distance from the historical median, in robust units.

    Median absolute deviation rather than standard deviation: the history may
    already contain the abuse we are trying to describe, and MAD does not let
    those windows widen the spread enough to hide themselves.

    MAD is zero whenever a feature is constant, which for these features is the
    normal case rather than an edge case. The fallback scales by a quarter of
    the median, making the comparison relative — a 2% move in a constant
    feature stays small while a 24x move does not. For a feature whose median
    is also zero (a project that only ever calls one endpoint) there is no
    relative scale to use, so it falls back to 1.0, which for entropy in bits
    is a meaningful unit on its own.
    """
    import numpy as np

    median = np.median(train, axis=0)
    mad = np.median(np.abs(train - median), axis=0)

    scale = np.where(
        mad > 0,
        mad,
        np.where(np.abs(median) > 0, np.abs(median) * 0.25, 1.0),
    )
    return np.abs(np.array(current.as_vector()) - median) / scale


def _rank_contributors(deviation: Any) -> list[str]:
    """The features that moved most, worst first, for the alert email."""
    ranked = sorted(zip(FEATURE_NAMES, deviation, strict=True), key=lambda p: -p[1])
    return [name for name, value in ranked if value >= DEVIATION_FLOOR][:3]
