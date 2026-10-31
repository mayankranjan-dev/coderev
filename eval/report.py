"""
Compute metrics from stored run artifacts and render a markdown report
(docs/EVAL_DESIGN.md §4-5).

    python -m eval.report <run_id> [<run_id> ...] --tolerance T [--write-results]

Scoring reads only eval/results/<run_id>/ and the case file; no model is
called. Each run_id holds every repeat (seed) of one config.

Definitions used here (EVAL_DESIGN §4; stated again in the report):

- Headline numbers use items whose fix commit is on or after the
  contamination cutoff only. Pre-cutoff items are reported separately.
- Recall = bug items with at least one catch / bug items attempted, per
  repeat. A review that failed (transport or unparseable output) counts as
  not caught; failure rates are reported next to it.
- A catch is decided by eval.match.is_catch (author-owned). Until it is
  implemented every recall figure is "TBD (matcher not implemented)".
- FP rate = control items with at least one CRITICAL finding / control items
  attempted. The secondary version counts any severity.
- Parse failure rate = reviews ending in malformed or empty output / reviews
  attempted. Transport failures are counted separately.
- Per-metric aggregation across repeats: mean, sample standard deviation,
  min, max. Item-sampling uncertainty: 95% percentile bootstrap over items
  (10,000 resamples, stdlib random, fixed seed) on the item-level value
  averaged over repeats. Paired comparison: paired bootstrap of the mean
  item-level difference; an interval that includes 0 is reported as
  "no measurable difference".
- Latency percentiles use linear interpolation between closest ranks.

--write-results writes docs/RESULTS.md, and refuses to if any run used the
stub provider or is a pilot run, so placeholder numbers can never be
published as results.
"""

import argparse
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from eval.cases import DEFAULT_CASES_PATH, CaseFileError, CaseSet, load_cases
from reviewer.engine import OUTPUT_KINDS, TRANSPORT_KINDS
from reviewer.findings import Finding

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_MD = ROOT / "docs" / "RESULTS.md"
TOLERANCE_SWEEP = (0, 3, 5, 10)
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260101
TBD_MATCHER = "TBD (matcher not implemented)"
TBD = "TBD (not yet measured)"

Matcher = Callable[[Finding, object, int], bool]


class MatcherUnavailable(Exception):
    pass


@dataclass
class Run:
    run_id: str
    path: Path
    config: dict
    env: dict
    records: list

    @property
    def seeds(self) -> list:
        return self.config.get("seeds", [])

    @property
    def repeats(self) -> list:
        return sorted({r["repeat"] for r in self.records})

    @property
    def is_stub(self) -> bool:
        return self.config.get("config", {}).get("provider") == "stub"

    @property
    def is_pilot(self) -> bool:
        return self.run_id.startswith("pilot-")


def load_run(results_dir: Path, run_id: str) -> Run:
    path = Path(results_dir) / run_id
    config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    env_path = path / "env.json"
    env = json.loads(env_path.read_text(encoding="utf-8")) if env_path.is_file() else {}
    records = [json.loads(line) for line in (path / "outputs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return Run(run_id, path, config, env, records)


# ---------------------------------------------------------------------------
# statistics helpers (stdlib only)
# ---------------------------------------------------------------------------


def percentile(values: list, q: float) -> Optional[float]:
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * q
    low, high = math.floor(pos), math.ceil(pos)
    return data[low] + (data[high] - data[low]) * (pos - low)


def spread(values: list) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"mean": None, "sd": None, "min": None, "max": None, "n": 0}
    return {
        "mean": statistics.fmean(values),
        "sd": statistics.stdev(values) if len(values) > 1 else None,
        "min": min(values),
        "max": max(values),
        "n": len(values),
    }


def bootstrap_ci(item_values: list, resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED) -> Optional[tuple]:
    """95% percentile bootstrap of the mean over items."""
    if not item_values:
        return None
    rng = random.Random(seed)
    n = len(item_values)
    means = sorted(statistics.fmean(rng.choices(item_values, k=n)) for _ in range(resamples))
    return percentile(means, 0.025), percentile(means, 0.975)


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def _findings(record) -> list:
    return [Finding.from_dict(f) for f in record.get("findings", [])]


def _located(f: Finding) -> bool:
    return f.file is not None and f.line_start is not None


def item_caught(record, label, matcher: Matcher, tolerance: int, critical_only: bool = False) -> bool:
    for f in _findings(record):
        if critical_only and f.severity != "CRITICAL":
            continue
        try:
            if matcher(f, label, tolerance):
                return True
        except NotImplementedError as exc:
            raise MatcherUnavailable(str(exc)) from None
    return False


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


def _per_item_mean(records_by_item: dict, fn: Callable) -> dict:
    """{case_id: mean over repeats of fn(record)} for item-level bootstrap."""
    return {cid: statistics.fmean(fn(r) for r in recs) for cid, recs in records_by_item.items() if recs}


def summarize(run: Run, case_set: CaseSet, matcher: Matcher, tolerance: int, include_pre_cutoff: bool = False) -> dict:
    cases = case_set.by_id()
    missing = sorted({r["case_id"] for r in run.records} - set(cases))
    if missing:
        raise ValueError(f"run {run.run_id} has cases not in the case file: {missing[:5]}")

    records = [r for r in run.records if include_pre_cutoff or not r.get("pre_cutoff")]
    production_timeout = run.config.get("config", {}).get("production_timeout_s")
    per_repeat = []
    matcher_ok = True

    for repeat in run.repeats:
        recs = [r for r in records if r["repeat"] == repeat]
        bugs = [r for r in recs if r["split"] == "bug"]
        controls = [r for r in recs if r["split"] == "control"]
        m = {"repeat": repeat}

        if matcher_ok:
            try:
                m["recall"] = _rate(sum(item_caught(r, cases[r["case_id"]].label, matcher, tolerance) for r in bugs), len(bugs))
                m["recall_critical"] = _rate(
                    sum(item_caught(r, cases[r["case_id"]].label, matcher, tolerance, True) for r in bugs), len(bugs)
                )
                for t in TOLERANCE_SWEEP:
                    m[f"recall_t{t}"] = _rate(sum(item_caught(r, cases[r["case_id"]].label, matcher, t) for r in bugs), len(bugs))
                off_target = 0
                for r in bugs:
                    label = cases[r["case_id"]].label
                    off_target += sum(1 for f in _findings(r) if not matcher(f, label, tolerance))
                m["off_target_findings"] = off_target
            except MatcherUnavailable:
                matcher_ok = False

        m["fp_rate_critical"] = _rate(sum(any(f.severity == "CRITICAL" for f in _findings(r)) for r in controls), len(controls))
        m["fp_rate_any"] = _rate(sum(bool(r.get("findings")) for r in controls), len(controls))
        for split_name, group in (("bug", bugs), ("control", controls)):
            m[f"findings_per_review_{split_name}"] = (
                statistics.fmean(len(r.get("findings", [])) for r in group) if group else None
            )
            for sev in ("CRITICAL", "WARNING", "NITPICK"):
                m[f"{sev.lower()}_per_review_{split_name}"] = (
                    statistics.fmean(sum(1 for f in r.get("findings", []) if f.get("severity") == sev) for r in group)
                    if group else None
                )
        all_findings = [f for r in recs for f in _findings(r)]
        m["unlocated_rate"] = _rate(sum(not _located(f) for f in all_findings), len(all_findings))
        m["parse_failure_rate"] = _rate(sum(r["status"] in OUTPUT_KINDS for r in recs), len(recs))
        m["transport_failure_rate"] = _rate(sum(r["status"] in TRANSPORT_KINDS for r in recs), len(recs))
        m["retries_mean"] = statistics.fmean(r.get("retries", 0) for r in recs) if recs else None
        m["truncation_rate"] = _rate(sum(bool(r.get("truncated")) for r in recs), len(recs))
        latencies = [r["latency_s"] for r in recs if r["status"] not in TRANSPORT_KINDS and r.get("latency_s")]
        m["latency_p50"] = percentile(latencies, 0.50)
        m["latency_p90"] = percentile(latencies, 0.90)
        m["latency_p95"] = percentile(latencies, 0.95)
        m["latency_mean"] = statistics.fmean(latencies) if latencies else None
        m["latency_sd"] = statistics.stdev(latencies) if len(latencies) > 1 else None
        m["over_production_timeout"] = (
            _rate(sum(v > production_timeout for v in latencies), len(latencies)) if production_timeout else None
        )
        m["n_bug"], m["n_control"], m["n_reviews"] = len(bugs), len(controls), len(recs)
        per_repeat.append(m)

    if not matcher_ok:
        for m in per_repeat:
            for key in [k for k in m if k.startswith("recall") or k == "off_target_findings"]:
                del m[key]

    metric_names = [k for k in per_repeat[0] if k != "repeat"] if per_repeat else []
    aggregate = {name: spread([m.get(name) for m in per_repeat]) for name in metric_names}

    # Item-level values for bootstrap and paired comparison.
    by_item_bug, by_item_ctl = {}, {}
    for r in records:
        (by_item_bug if r["split"] == "bug" else by_item_ctl).setdefault(r["case_id"], []).append(r)
    items = {"fp_rate_critical": _per_item_mean(by_item_ctl, lambda r: float(any(f.get("severity") == "CRITICAL" for f in r.get("findings", []))))}
    if matcher_ok:
        items["recall"] = _per_item_mean(
            by_item_bug, lambda r: float(item_caught(r, cases[r["case_id"]].label, matcher, tolerance))
        )
    ci = {name: bootstrap_ci(list(values.values())) for name, values in items.items()}

    retry_distribution = {}
    for r in records:
        retry_distribution[r.get("retries", 0)] = retry_distribution.get(r.get("retries", 0), 0) + 1

    categories = {}
    if matcher_ok:
        for r in records:
            if r["split"] != "bug":
                continue
            label = cases[r["case_id"]].label
            cat = categories.setdefault(label.category, {"items": set(), "caught": 0, "reviews": 0})
            cat["items"].add(r["case_id"])
            cat["reviews"] += 1
            cat["caught"] += item_caught(r, label, matcher, tolerance)
        categories = {k: {"n_items": len(v["items"]), "recall": v["caught"] / v["reviews"]} for k, v in sorted(categories.items())}

    pre_cutoff_ids = sorted({r["case_id"] for r in run.records if r.get("pre_cutoff")})
    return {
        "run_id": run.run_id,
        "config_name": run.config.get("config", {}).get("name"),
        "config_hash": run.config.get("config_hash"),
        "provider": run.config.get("config", {}).get("provider"),
        "model": run.config.get("config", {}).get("model"),
        "seeds": run.seeds,
        "tolerance": tolerance,
        "matcher_implemented": matcher_ok,
        "headline_excludes_pre_cutoff": not include_pre_cutoff,
        "pre_cutoff_items": pre_cutoff_ids,
        "per_repeat": per_repeat,
        "aggregate": aggregate,
        "bootstrap_95": {k: list(v) if v else None for k, v in ci.items()},
        "item_values": {k: v for k, v in items.items()},
        "retry_distribution": dict(sorted(retry_distribution.items())),
        "recall_by_category": categories,
        "dataset_blob_hash": run.env.get("dataset_blob_hash"),
    }


def paired_comparison(a: dict, b: dict, metric: str) -> Optional[dict]:
    """Paired bootstrap of mean(item_a - item_b) over items present in both."""
    va, vb = a["item_values"].get(metric), b["item_values"].get(metric)
    if va is None or vb is None:
        return None
    common = sorted(set(va) & set(vb))
    if not common:
        return None
    diffs = [va[c] - vb[c] for c in common]
    low, high = bootstrap_ci(diffs)
    return {
        "metric": metric,
        "n_items": len(common),
        "mean_diff": statistics.fmean(diffs),
        "ci95": [low, high],
        "verdict": "no measurable difference" if low <= 0 <= high else ("A higher" if low > 0 else "B higher"),
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

ROWS = [
    ("Recall", "recall", "pct"),
    ("Recall, CRITICAL only", "recall_critical", "pct"),
    *[(f"Recall at tolerance {t}", f"recall_t{t}", "pct") for t in TOLERANCE_SWEEP],
    ("FP rate on controls (CRITICAL)", "fp_rate_critical", "pct"),
    ("FP rate on controls (any severity)", "fp_rate_any", "pct"),
    ("Findings per review, bug items", "findings_per_review_bug", "num"),
    ("Findings per review, controls", "findings_per_review_control", "num"),
    ("Off-target findings on bug items (count)", "off_target_findings", "num"),
    ("Unlocated finding rate", "unlocated_rate", "pct"),
    ("Parse failure rate", "parse_failure_rate", "pct"),
    ("Transport failure rate", "transport_failure_rate", "pct"),
    ("Retries per review (mean)", "retries_mean", "num"),
    ("Truncation suspected", "truncation_rate", "pct"),
    ("Latency p50 (s)", "latency_p50", "num"),
    ("Latency p90 (s)", "latency_p90", "num"),
    ("Latency p95 (s)", "latency_p95", "num"),
    ("Latency mean (s)", "latency_mean", "num"),
    ("Over production timeout", "over_production_timeout", "pct"),
]


def _fmt(value, kind) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%" if kind == "pct" else f"{value:.2f}"


def _cell(summary: dict, key: str, kind: str) -> str:
    if key.startswith("recall") or key == "off_target_findings":
        if not summary["matcher_implemented"]:
            return TBD_MATCHER
    agg = summary["aggregate"].get(key)
    if not agg or agg["mean"] is None:
        return "n/a"
    text = _fmt(agg["mean"], kind)
    if agg["sd"] is not None:
        text += f" ± {_fmt(agg['sd'], kind)}"
    if agg["n"] > 1:
        text += f" [{_fmt(agg['min'], kind)}, {_fmt(agg['max'], kind)}]"
    ci = summary["bootstrap_95"].get(key)
    if ci:
        text += f"; 95% CI {_fmt(ci[0], 'pct')}–{_fmt(ci[1], 'pct')}"
    return text


def render_markdown(summaries: list, comparisons: list, command: str, generated_at: str) -> str:
    lines = [
        "| Metric | " + " | ".join(f"{s['config_name']} (`{s['run_id']}`)" for s in summaries) + " |",
        "|---|" + "---|" * len(summaries),
    ]
    for title, key, kind in ROWS:
        lines.append(f"| {title} | " + " | ".join(_cell(s, key, kind) for s in summaries) + " |")
    first = summaries[0]
    counts = first["per_repeat"][0] if first["per_repeat"] else {}
    out = [
        f"Generated {generated_at} by `{command}`.",
        "",
        f"Cells: mean ± sample SD [min, max] across {len(first['seeds'])} repeat(s) (seeds {first['seeds']}); "
        "95% CI is a bootstrap over items. Tolerance for headline recall: "
        f"{first['tolerance']} line(s). Headline numbers exclude {len(first['pre_cutoff_items'])} pre-cutoff item(s). "
        f"Items per repeat: {counts.get('n_bug', 0)} bug, {counts.get('n_control', 0)} control.",
        "",
        *lines,
    ]
    if comparisons:
        out += ["", "Paired comparisons (A − B, paired bootstrap over shared items):", "",
                "| A | B | Metric | Items | Mean difference | 95% CI | Verdict |", "|---|---|---|---|---|---|---|"]
        for a, b, c in comparisons:
            if c is None:
                out.append(f"| {a} | {b} | | | | | {TBD_MATCHER} |")
                continue
            out.append(
                f"| {a} | {b} | {c['metric']} | {c['n_items']} | {c['mean_diff'] * 100:+.1f} pp | "
                f"{c['ci95'][0] * 100:+.1f} to {c['ci95'][1] * 100:+.1f} pp | {c['verdict']} |"
            )
    if any(s["recall_by_category"] for s in summaries):
        out += ["", "Recall by category (pooled over repeats; small n, no claims):", ""]
        for s in summaries:
            for cat, v in s["recall_by_category"].items():
                out.append(f"- {s['config_name']} / {cat}: {v['recall'] * 100:.1f}% (n = {v['n_items']} items)")
    return "\n".join(out) + "\n"


RESULTS_HEADER = "# Evaluation Results\n\n"


def tbd_results_md() -> str:
    return (
        RESULTS_HEADER
        + f"Status: {TBD}. No evaluation run has been recorded in this file yet.\n\n"
        "This file is written by `python -m eval.report <run_id> ... --tolerance <T> --write-results`, "
        "which records the exact command and date. It refuses stub-provider and pilot runs. "
        "See `docs/eval/AUTHOR_TODO.md` for the steps that must happen first.\n\n"
        "| Metric | Value |\n|---|---|\n"
        f"| Recall | {TBD} |\n| FP rate on controls | {TBD} |\n| Findings per review | {TBD} |\n"
        f"| Parse failure rate | {TBD} |\n| Latency p50 / p95 | {TBD} |\n"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Report metrics for one or more eval runs.")
    parser.add_argument("run_ids", nargs="+")
    parser.add_argument("--tolerance", type=int, required=True, help="line tolerance for headline recall")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--include-pre-cutoff", action="store_true", help="contamination probe; never for headlines")
    parser.add_argument("--write-results", action="store_true", help=f"write {RESULTS_MD.relative_to(ROOT)}")
    parser.add_argument("--out", help="also write the markdown table to this file")
    args = parser.parse_args(argv)

    if args.tolerance < 0:
        print("--tolerance must be >= 0", file=sys.stderr)
        return 2
    try:
        case_set = load_cases(args.cases)
    except CaseFileError as exc:
        print(exc, file=sys.stderr)
        return 2
    from eval.match import is_catch

    runs = [load_run(Path(args.results_dir), rid) for rid in args.run_ids]
    for run in runs:
        if run.env.get("dataset_blob_hash") and run.env["dataset_blob_hash"] != case_set.blob_hash:
            print(f"{run.run_id}: was run on a different case file (blob {run.env['dataset_blob_hash']}); "
                  "re-run it on the current dataset.", file=sys.stderr)
            return 2

    summaries = [summarize(r, case_set, is_catch, args.tolerance, args.include_pre_cutoff) for r in runs]
    for run, summary in zip(runs, summaries):
        (run.path / "summary.json").write_text(json.dumps(summary, indent=2, default=list), encoding="utf-8")

    comparisons = []
    for other in summaries[1:]:
        for metric in ("recall", "fp_rate_critical"):
            comparisons.append((summaries[0]["config_name"], other["config_name"], paired_comparison(summaries[0], other, metric)))

    command = "python -m eval.report " + " ".join(argv if argv is not None else sys.argv[1:])
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    table = render_markdown(summaries, comparisons, command, generated)
    print(table)
    if args.out:
        Path(args.out).write_text(table, encoding="utf-8")
    if args.write_results:
        bad = [r.run_id for r in runs if r.is_stub or r.is_pilot]
        if bad:
            print(f"refusing to write {RESULTS_MD.name}: stub or pilot run(s) {bad} are not results.", file=sys.stderr)
            return 1
        RESULTS_MD.write_text(RESULTS_HEADER + table, encoding="utf-8")
        print(f"wrote {RESULTS_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
