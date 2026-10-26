"""Tier benchmark: runs the current hype algorithm on benchmark/tier_benchmark.json inputs and reports
agreement SEPARATELY for manual_review (independent ground truth) and algorithm_echo (regression only).
Usage: python -m scripts.benchmark_tiers  (or python scripts/benchmark_tiers.py)"""
import json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hypebot.config import Settings, load_weights
from hypebot.external_x import to_x_data
from hypebot.metrics import _dt
from hypebot.pipeline import evaluate


def run(path=ROOT / "benchmark" / "tier_benchmark.json") -> dict:
    W = load_weights(str(ROOT / "weights.json"))
    out = {"manual_review": {"n": 0, "agree": 0, "rows": []}, "algorithm_echo": {"n": 0, "agree": 0, "rows": []},
           "unknown": {"n": 0, "agree": 0, "rows": []}}
    for c in json.loads(pathlib.Path(path).read_text())["cases"]:
        p = json.loads((ROOT / c["input_file"]).read_text())
        _, r, _ = evaluate(Settings(weights=W), c["name"], to_x_data(p, c["handle"]), _dt(p["collected_at"]), [])
        src = c.get("ground_truth_source", "unknown")
        exp = (c.get("manual_review") or {}).get("manual_tier") if src == "manual_review" else c.get("expected_tier")
        ok = (r.tier if r.ranked else None) == exp
        b = out[src]; b["n"] += 1; b["agree"] += ok
        b["rows"].append({"name": c["name"], "expected": exp, "bot_tier": r.tier if r.ranked else None,
                          "score": r.hype_score, "confidence": r.confidence_label, "agree": ok})
    return out


if __name__ == "__main__":
    res = run()
    for k, b in res.items():
        print(f"{k}: {b['agree']}/{b['n']} agree" + ("  (regression only — NOT accuracy)" if k == "algorithm_echo" else ""))
        for row in b["rows"]:
            print("   ", row)
    if not res["manual_review"]["n"]:
        print("NO manual ground truth yet — accuracy cannot be measured.")
