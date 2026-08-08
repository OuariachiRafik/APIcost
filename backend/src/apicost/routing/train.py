"""Offline training for the tier classifier — BUILD_SPEC §4 P5.

    uv run python -m apicost.routing.train

Reads the labelled seed dataset in ``seed_dataset.py``, extracts features with
the **same** ``extract_features`` the proxy uses, fits a calibrated logistic
regression, and writes a versioned joblib artifact.

Retraining on real data is the point of this file existing rather than a
notebook. Once there is escalation-outcome history, the honest training signal
is: requests routed to cheap that were *not* escalated were genuinely cheap;
requests that were escalated needed a stronger tier. That is a real label
derived from production, and it is far better than the hand-labelling below.
Export it from `requests_log` and pass `--from-history`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from apicost.routing.features import FEATURE_NAMES, extract_features, to_vector
from apicost.routing.seed_dataset import SEED_EXAMPLES


def build_dataset() -> tuple[list[list[float]], list[str]]:
    vectors: list[list[float]] = []
    labels: list[str] = []
    for prompt, model, tier in SEED_EXAMPLES:
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        vectors.append(to_vector(extract_features(body)))
        labels.append(tier)
    return vectors, labels


def train(output: Path, *, seed: int = 17) -> dict[str, object]:
    try:
        import joblib
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        print("scikit-learn is required: uv sync --group ml", file=sys.stderr)
        raise SystemExit(2) from None

    vectors, labels = build_dataset()
    print(f"  {len(vectors)} examples, {len(set(labels))} classes")

    base = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=2000, C=1.0, class_weight="balanced", random_state=seed),
    )
    # Calibrated because the pipeline gates on a probability threshold, and an
    # uncalibrated logistic regression's "0.8" is not really 80%.
    # ensemble=False fits ONE calibrated model over cross-validated predictions,
    # rather than keeping five and averaging them at predict time. Measured on
    # this hardware: the five-model form cost ~95 ms per prediction against a
    # 20 ms routing budget, which would have made the router fail open on every
    # request — working in tests, never routing in production. Calibration is
    # kept; only the redundant ensemble goes.
    model = CalibratedClassifierCV(base, cv=5, method="sigmoid", ensemble=False)

    scores = cross_val_score(model, vectors, labels, cv=5, scoring="accuracy")
    print(f"  5-fold accuracy: {scores.mean():.3f} (+/- {scores.std():.3f})")

    model.fit(vectors, labels)

    version = datetime.now(UTC).strftime("seed-%Y%m%d-%H%M")
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "feature_names": FEATURE_NAMES,
            "version": version,
            "trained_at": datetime.now(UTC).isoformat(),
            "examples": len(vectors),
            "cv_accuracy": float(scores.mean()),
        },
        output,
    )
    print(f"  wrote {output} (version {version})")

    return {"version": version, "accuracy": float(scores.mean()), "examples": len(vectors)}


def main() -> int:
    from apicost.routing.classifier import artifact_path

    parser = argparse.ArgumentParser(description="Train the routing tier classifier")
    parser.add_argument("--output", type=Path, default=artifact_path())
    args = parser.parse_args()

    print("training tier classifier:")
    train(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
