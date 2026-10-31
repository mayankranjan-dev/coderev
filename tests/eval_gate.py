"""
Synthetic eval regression gate (CI).

THIS GATE COVERS THE HARNESS, NOT THE REAL MODEL. It runs the eval pipeline
(case loader, fetcher cache, engine, parser, report) on the 6 synthetic cases
from tests/test_eval_harness.py with the stub provider and the TEST-ONLY
matcher, then compares the metrics to eval/baseline.json. A failure means the
harness or the shared review code changed behavior. It says nothing about how
well any model finds bugs, and it does not use the author-owned
eval.match.is_catch.

    python tests/eval_gate.py --check     # CI: exit 1 if a metric got worse
    python tests/eval_gate.py --update    # rewrite eval/baseline.json from a real run

Direction of "worse": a HIGHER_IS_BETTER metric may not drop and a
LOWER_IS_BETTER metric may not rise, both beyond EPSILON.
"""
import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for entry in (ROOT, ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from eval import report  # noqa: E402
from eval.config import from_dict  # noqa: E402
from eval.run import prepare, run_config  # noqa: E402
from reviewer.providers import StubProvider  # noqa: E402
from test_eval_harness import CASES, build_workspace, label_reply, test_only_matcher  # noqa: E402

BASELINE_PATH = ROOT / "eval" / "baseline.json"
COMMAND = "python tests/eval_gate.py --update"
SEEDS = (1, 2, 3)
TOLERANCE = 0
EPSILON = 1e-9

HIGHER_IS_BETTER = ("recall", "recall_critical")
LOWER_IS_BETTER = ("fp_rate_critical", "fp_rate_any", "unlocated_rate", "parse_failure_rate",
                   "transport_failure_rate", "truncation_rate")


def _perfect(prompt, seed):
    return label_reply(prompt, seed)


def _seed_dependent(prompt, seed):
    # Hits on odd seeds, silent on even ones.
    return label_reply(prompt, seed) if seed % 2 else "NO_ISSUES_FOUND"


def _flood_misses(prompt, seed):
    path = next((p for _, _, p, _, _ in CASES if f"+++ b/{p}" in prompt), "x.py")
    return "\n".join(f"CRITICAL: {path}:{900 + i} noise Correction: n" for i in range(50))


def _unlocated(prompt, seed):
    return "CRITICAL: something is wrong Correction: fix it"


SCENARIOS = {
    "perfect": _perfect,
    "seed_dependent": _seed_dependent,
    "flood_misses": _flood_misses,
    "unlocated": _unlocated,
}


def measure() -> dict:
    """Run every scenario on a fresh synthetic workspace and return its metrics."""
    result = {}
    with tempfile.TemporaryDirectory() as tmp:
        ws = build_workspace(Path(tmp))
        for name, reply in SCENARIOS.items():
            config = from_dict({"name": name, "provider": "stub", "warmup": False})
            case_set, fetched, errors = prepare(config, ws["cases"], ws["cache"], offline=True)
            if errors:
                raise SystemExit(f"synthetic workspace failed to load: {errors}")
            asyncio.run(run_config(config, case_set, fetched, ws["results"] / name, name, SEEDS,
                                   provider=StubProvider(replies=[reply]), command=["eval_gate"]))
            run = report.load_run(ws["results"], name)
            aggregate = report.summarize(run, case_set, test_only_matcher, TOLERANCE)["aggregate"]
            result[name] = {key: aggregate[key]["mean"] for key in HIGHER_IS_BETTER + LOWER_IS_BETTER}
            result[name]["n_reviews"] = aggregate["n_reviews"]["mean"]
    return result


def compare(baseline: dict, current: dict) -> list:
    problems = []
    for scenario, expected in baseline["scenarios"].items():
        got = current.get(scenario)
        if got is None:
            problems.append(f"{scenario}: scenario missing from current run")
            continue
        if got["n_reviews"] != expected["n_reviews"]:
            problems.append(f"{scenario}: n_reviews {expected['n_reviews']} -> {got['n_reviews']}")
        for key in HIGHER_IS_BETTER:
            if got[key] is None or expected[key] is None or got[key] < expected[key] - EPSILON:
                problems.append(f"{scenario}: {key} dropped {expected[key]} -> {got[key]}")
        for key in LOWER_IS_BETTER:
            if got[key] is None or expected[key] is None or got[key] > expected[key] + EPSILON:
                problems.append(f"{scenario}: {key} rose {expected[key]} -> {got[key]}")
    return problems


def baseline_document(scenarios: dict) -> dict:
    return {
        "_comment": (
            "Baseline for the synthetic eval regression gate. It covers the HARNESS (stub provider, 6 synthetic "
            "cases, test-only matcher), NOT the real model. Regenerate only when harness behavior is changed on "
            "purpose, and say why in the commit."
        ),
        "command": COMMAND,
        "seeds": list(SEEDS),
        "tolerance": TOLERANCE,
        "cases": [c[0] for c in CASES],
        "scenarios": scenarios,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--update", action="store_true")
    args = parser.parse_args(argv)

    current = measure()
    if args.update:
        BASELINE_PATH.write_text(json.dumps(baseline_document(current), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE_PATH.relative_to(ROOT)}")
        return 0

    if not BASELINE_PATH.is_file():
        print(f"missing {BASELINE_PATH.relative_to(ROOT)}; create it with: {COMMAND}", file=sys.stderr)
        return 2
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    problems = compare(baseline, current)
    for problem in problems:
        print(f"REGRESSION {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"synthetic eval gate: {len(current)} scenarios match the baseline (harness only, not the model)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
