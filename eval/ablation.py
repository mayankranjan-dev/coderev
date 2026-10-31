"""
Run the ablation matrix and write one combined table (EVAL_DESIGN §5.2).

    python -m eval.ablation [--matrix eval/ablation.yaml] [--offline] [--only NAME ...] [--dry-run]

Runs the baseline and every variant with the same seeds over the same cases,
then renders one markdown table (eval/report.py) with a paired comparison of
each variant against the baseline. The table is written to
eval/results/ablation-<timestamp>.md and printed. It is not written to
docs/RESULTS.md; publish with `python -m eval.report ... --write-results`.

--dry-run swaps in the stub provider to check the pipeline end to end
without Ollama. Dry-run output is not a result.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx
import yaml

from eval import report
from eval.cases import DEFAULT_CASES_PATH, CaseFileError
from eval.config import ConfigError, RunConfig, load_config, seeds_for
from eval.fetch import DEFAULT_CACHE_DIR
from eval.run import DEFAULT_RESULTS_DIR, make_run_id, prepare, run_config

FACTORS = ("model", "prompt_version", "chunking", "retry")
DEFAULT_MATRIX = Path(__file__).resolve().parent / "ablation.yaml"


class MatrixError(ValueError):
    pass


@dataclass
class Planned:
    name: str
    config: RunConfig
    skipped: Optional[str] = None
    run_id: Optional[str] = None


def load_matrix(path) -> dict:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise MatrixError(f"matrix file not found: {path}") from None
    unknown = sorted(set(data) - {"baseline", "repeats", "tolerance", "variants"})
    if unknown:
        raise MatrixError(f"unknown matrix field(s): {', '.join(unknown)}")
    if "baseline" not in data:
        raise MatrixError("matrix needs a 'baseline' config path")
    tolerance = data.get("tolerance")
    if not (isinstance(tolerance, int) and not isinstance(tolerance, bool) and tolerance >= 0):
        raise MatrixError("matrix needs 'tolerance': a non-negative integer")
    baseline_path = (path.parent / data["baseline"]).resolve()
    baseline = load_config(baseline_path)

    variants, names = [], {"baseline"}
    for index, variant in enumerate(data.get("variants") or []):
        if not isinstance(variant, dict) or set(variant) - {"name", "set"} or "name" not in variant:
            raise MatrixError(f"variants[{index}] must have exactly 'name' and 'set'")
        name, changes = variant["name"], variant.get("set") or {}
        if name in names:
            raise MatrixError(f"duplicate variant name {name!r}")
        names.add(name)
        bad = sorted(set(changes) - set(FACTORS))
        if bad:
            raise MatrixError(f"variant {name!r} changes non-factor field(s) {bad}; factors are {FACTORS}")
        changed = [k for k, v in changes.items() if getattr(baseline, k) != v]
        if len(changed) != 1:
            raise MatrixError(f"variant {name!r} must change exactly one factor from the baseline, changes {changed}")
        variants.append((name, baseline.with_overrides(name=name, **changes)))
    return {"baseline": baseline, "variants": variants, "repeats": data.get("repeats"), "tolerance": tolerance}


def ollama_models(base_url: str) -> Optional[set]:
    """Names of pulled models, or None if Ollama cannot be asked."""
    try:
        with httpx.Client(timeout=5.0) as client:
            models = client.get(f"{base_url}/api/tags").json().get("models", [])
        return {m.get("name") for m in models} | {m.get("model") for m in models}
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


def plan(matrix: dict, only: Optional[list], dry_run: bool, available: Callable[[str], Optional[set]]) -> list:
    entries = [("baseline", matrix["baseline"]), *matrix["variants"]]
    if only:
        unknown = sorted(set(only) - {n for n, _ in entries})
        if unknown:
            raise MatrixError(f"--only names not in the matrix: {unknown}")
        entries = [(n, c) for n, c in entries if n in only or n == "baseline"]
    planned = []
    models = None if dry_run else available(matrix["baseline"].base_url)
    for name, cfg in entries:
        if dry_run:
            planned.append(Planned(name, cfg.with_overrides(provider="stub", warmup=False)))
            continue
        if models is not None and cfg.model not in models:
            planned.append(Planned(name, cfg, skipped=f"model {cfg.model} is not pulled in Ollama"))
            continue
        planned.append(Planned(name, cfg))
    return planned


def run_matrix(matrix: dict, planned: list, cases_path, cache_dir, results_dir: Path, offline: bool,
               provider_for=None, progress=print) -> list:
    seeds = seeds_for(matrix["baseline"], matrix["repeats"])
    fetched_cache = {}
    for entry in planned:
        if entry.skipped:
            progress(f"SKIP {entry.name}: {entry.skipped}")
            continue
        key = (entry.config.context_lines, entry.config.include_tests, entry.config.cutoff)
        if key not in fetched_cache:
            case_set, fetched, errors = prepare(entry.config, cases_path, cache_dir, offline)
            if errors:
                raise MatrixError("case fetch/validation failed: " + "; ".join(f"{k}: {v}" for k, v in sorted(errors.items())))
            fetched_cache[key] = (case_set, fetched)
        case_set, fetched = fetched_cache[key]
        entry.run_id = make_run_id(entry.config)
        if entry.config.provider == "stub":
            entry.run_id = f"dryrun-{entry.run_id}"
        provider = provider_for(entry.config) if provider_for else None
        progress(f"RUN {entry.name} -> {entry.run_id}")
        asyncio.run(run_config(entry.config, case_set, fetched, results_dir / entry.run_id, entry.run_id,
                               seeds, provider=provider, progress=progress))
    return planned


def combined_table(planned: list, cases_path, results_dir: Path, tolerance: int, command: str, matcher=None) -> str:
    from eval.cases import load_cases
    from eval.match import is_catch

    case_set = load_cases(cases_path)
    ran = [p for p in planned if p.run_id]
    summaries = [report.summarize(report.load_run(results_dir, p.run_id), case_set, matcher or is_catch, tolerance)
                 for p in ran]
    comparisons = []
    base = next((s for s, p in zip(summaries, ran) if p.name == "baseline"), None)
    if base is not None:
        for s, p in zip(summaries, ran):
            if p.name != "baseline":
                for metric in ("recall", "fp_rate_critical"):
                    comparisons.append((p.name, "baseline", report.paired_comparison(s, base, metric)))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    table = report.render_markdown(summaries, comparisons, command, generated) if summaries else "No configuration ran.\n"
    skipped = [p for p in planned if p.skipped]
    if skipped:
        table += "\nSkipped:\n\n" + "".join(f"- {p.name}: {p.skipped}\n" for p in skipped)
    return table


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the ablation matrix.")
    parser.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--only", nargs="+", help="run only these variants (plus the baseline)")
    parser.add_argument("--dry-run", action="store_true", help="stub provider; checks the pipeline, produces no results")
    args = parser.parse_args(argv)

    try:
        matrix = load_matrix(args.matrix)
        planned = plan(matrix, args.only, args.dry_run, ollama_models)
        results_dir = Path(args.results_dir)
        run_matrix(matrix, planned, args.cases, Path(args.cache_dir), results_dir, args.offline)
        command = "python -m eval.ablation " + " ".join(argv if argv is not None else sys.argv[1:])
        table = combined_table(planned, args.cases, results_dir, matrix["tolerance"], command)
    except (MatrixError, ConfigError, CaseFileError) as exc:
        print(exc, file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = "dryrun-ablation" if args.dry_run else "ablation"
    out = results_dir / f"{prefix}-{stamp}.md"
    results_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(table, encoding="utf-8")
    print(table)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
