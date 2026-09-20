#!/usr/bin/env python3
"""
HardTruth Evaluation Harness & Threshold Calibration
Evaluates DeBERTa-v3 cross-encoder on evals/dataset.jsonl across thresholds tau in [0.10, 0.95].
Measures True Positives, False Positives, True Negatives, False Negatives, FPR, FNR, Precision, Recall, and F1.
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

DATASET_PATH = os.path.join(os.path.dirname(__file__), "dataset.jsonl")

def load_dataset(path: str = DATASET_PATH) -> list:
    pairs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                pairs.append(json.loads(line))
    return pairs

def evaluate_pair_http(daemon_url: str, premise: str, hypothesis: str) -> dict:
    url = f"{daemon_url.rstrip('/')}/v1/verify-claim"
    payload = json.dumps({"premise": premise, "hypothesis": hypothesis}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return json.loads(resp.read().decode("utf-8"))

def score_dataset(pairs: list, daemon_url: str = "http://127.0.0.1:8000", max_workers: int = 8) -> list:
    """
    Evaluates all pairs and caches model contradiction probabilities.
    """
    print(f"Scoring {len(pairs)} pairs against daemon at {daemon_url} (workers={max_workers})...")
    scored = []
    t0 = time.perf_counter()

    def _eval(item):
        try:
            res = evaluate_pair_http(daemon_url, item["premise"], item["hypothesis"])
            contradiction_prob = res.get("probabilities", {}).get("contradiction", 0.0)
            return {
                "id": item["id"],
                "category": item["category"],
                "ground_truth": item["ground_truth"],
                "language": item.get("language", "en"),
                "contradiction_prob": contradiction_prob
            }
        except Exception as e:
            print(f"Error evaluating {item['id']}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = pool.map(_eval, pairs)

    for r in results:
        if r is not None:
            scored.append(r)

    elapsed = time.perf_counter() - t0
    print(f"Scored {len(scored)} pairs in {elapsed:.2f}s ({len(scored)/elapsed:.1f} pairs/sec).")
    return scored

def sweep_thresholds(scored_pairs: list):
    """
    Sweeps tau in [0.10, 0.95] in steps of 0.05.
    Positive condition: Claim is a CONTRADICTION (model predicts contradiction >= tau).
    Negative condition: Claim is NOT_CONTRADICTION (model predicts contradiction < tau).
    """
    thresholds = [round(t * 0.05, 2) for t in range(2, 20)]  # 0.10 to 0.95

    print("\n" + "=" * 88)
    print(f"{'tau':^6} | {'TP':^5} | {'FP':^5} | {'TN':^5} | {'FN':^5} | {'FPR':^8} | {'FNR':^8} | {'Prec':^7} | {'Recall':^7} | {'F1':^7}")
    print("-" * 88)

    best_tau = None
    best_f1 = -1.0
    sweep_results = []

    for tau in thresholds:
        tp = 0  # Ground truth CONTRADICTION, predicted CONTRADICTION
        fp = 0  # Ground truth NOT_CONTRADICTION, predicted CONTRADICTION
        tn = 0  # Ground truth NOT_CONTRADICTION, predicted NOT_CONTRADICTION
        fn = 0  # Ground truth CONTRADICTION, predicted NOT_CONTRADICTION

        for p in scored_pairs:
            is_positive_gt = (p["ground_truth"] == "CONTRADICTION")
            predicted_positive = (p["contradiction_prob"] >= tau)

            if is_positive_gt and predicted_positive:
                tp += 1
            elif not is_positive_gt and predicted_positive:
                fp += 1
            elif not is_positive_gt and not predicted_positive:
                tn += 1
            elif is_positive_gt and not predicted_positive:
                fn += 1

        total_p = tp + fn
        total_n = tn + fp

        fpr = (fp / total_n) if total_n > 0 else 0.0
        fnr = (fn / total_p) if total_p > 0 else 0.0
        prec = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
        rec = (tp / total_p) if total_p > 0 else 0.0
        f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0

        sweep_results.append({
            "tau": tau,
            "tp": tp,
            "fp": fp,
            "tn": tn,
            "fn": fn,
            "fpr": fpr,
            "fnr": fnr,
            "prec": prec,
            "rec": rec,
            "f1": f1
        })

        marker = ""
        if f1 > best_f1:
            best_f1 = f1
            best_tau = tau

        print(f"{tau:6.2f} | {tp:5d} | {fp:5d} | {tn:5d} | {fn:5d} | {fpr:7.2%} | {fnr:7.2%} | {prec:7.3f} | {rec:7.3f} | {f1:7.3f}")

    print("=" * 88)
    print(f"\nCalibrated Threshold tau*: {best_tau:.2f} (Max F1: {best_f1:.4f})")

    # Category breakdowns at best_tau
    print(f"\nBreakdown at tau* = {best_tau:.2f}:")
    for cat in ["verified_truth", "false_claim", "meta_discussion"]:
        cat_items = [p for p in scored_pairs if p["category"] == cat]
        cat_pred_contra = sum(1 for p in cat_items if p["contradiction_prob"] >= best_tau)
        cat_pct = (cat_pred_contra / len(cat_items)) * 100 if cat_items else 0.0
        print(f"  - Category '{cat}' ({len(cat_items)} pairs): {cat_pred_contra} flagged as contradiction ({cat_pct:.1f}%)")

    return best_tau, sweep_results

def main():
    parser = argparse.ArgumentParser(description="HardTruth Threshold Calibration Sweep")
    parser.add_argument("--daemon-url", default="http://127.0.0.1:8000", help="Daemon API URL")
    parser.add_argument("--workers", type=int, default=8, help="Parallel worker threads")
    parser.add_argument("--dataset", default=DATASET_PATH, help="Path to dataset.jsonl")
    parser.add_argument("--output", default=None, help="Path to save sweep results JSON")
    args = parser.parse_args()

    pairs = load_dataset(args.dataset)
    scored = score_dataset(pairs, daemon_url=args.daemon_url, max_workers=args.workers)
    best_tau, results = sweep_thresholds(scored)

    if args.output:
        with open(args.output, "w") as f:
            json.dump({"calibrated_tau": best_tau, "results": results}, f, indent=2)
        print(f"\nResults saved to {args.output}")

if __name__ == "__main__":
    main()
