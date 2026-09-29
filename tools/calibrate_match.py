"""Calibrate feature weights against human-labeled photos.

Usage: python tools/calibrate_match.py validation/labels.jsonl [--write]
The input stays local. No uploaded photo is persisted by the web service.
"""

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from match_engine import EXTRACTOR, FEATURES, FishMatcher  # noqa: E402


def evaluate(matcher, samples, weights):
    distances = np.sum(weights * np.abs(matcher.features[None, :, :] - samples["features"][:, None, :]), axis=2) / weights.sum()
    ranked = np.argsort(distances, axis=1, kind="stable")[:, :3]
    expected = samples["best"]
    acceptable = samples["acceptable"]
    top1 = sum(matcher.ids[int(row[0])] == best for row, best in zip(ranked, expected)) / len(expected)
    top3 = sum(any(matcher.ids[int(i)] in choices for i in row) for row, choices in zip(ranked, acceptable)) / len(expected)
    return top1, top3


def load_labels(path, matcher):
    vectors, best, acceptable = [], [], []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        image_path = (path.parent / row["photo"]).resolve()
        if row["best"] not in matcher.ids or any(value not in matcher.ids for value in row.get("alternatives", [])):
            raise ValueError(f"line {line_number}: invalid fish ID")
        with Image.open(image_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((2048, 2048))
        face, _ = EXTRACTOR.detect(image)
        features = EXTRACTOR.features(image, face)
        vectors.append([features[key] for key in FEATURES])
        best.append(row["best"])
        acceptable.append(set([row["best"], *row.get("alternatives", [])]))
    if len(vectors) < 20:
        raise ValueError("at least 20 human-labeled photos are required for calibration")
    return {"features": np.array(vectors, dtype=np.float32), "best": best, "acceptable": acceptable}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("labels", type=Path)
    parser.add_argument("--write", action="store_true", help="save tuned weights to fish_features.json")
    args = parser.parse_args()
    matcher = FishMatcher()
    samples = load_labels(args.labels, matcher)
    weights = matcher.weights.copy()
    baseline = evaluate(matcher, samples, weights)
    # Coordinate search uses only explicit facial features; its small regularizer
    # discourages drastic changes on small validation sets.
    for _ in range(3):
        for index in range(len(FEATURES)):
            candidates = []
            for value in np.arange(.5, 3.01, .25):
                trial = weights.copy(); trial[index] = value
                top1, top3 = evaluate(matcher, samples, trial)
                objective = top1 + .25 * top3 - .01 * np.abs(trial - matcher.weights).sum()
                candidates.append((objective, -abs(value - matcher.weights[index]), value))
            weights[index] = max(candidates)[2]
    tuned = evaluate(matcher, samples, weights)
    print(f"n={len(samples['best'])} baseline top1={baseline[0]:.3f} acceptable@3={baseline[1]:.3f}")
    print(f"in-sample tuned top1={tuned[0]:.3f} acceptable@3={tuned[1]:.3f}; confirm on held-out photos")
    print(dict(zip(FEATURES, [float(value) for value in weights])))
    if args.write:
        catalog_path = Path(__file__).resolve().parents[1] / "fish_features.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["weights"] = dict(zip(FEATURES, [float(value) for value in weights]))
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
