"""
Run one configuration over all cases with N repeats (docs/EVAL_DESIGN.md §5).

    python -m eval.run --config eval/configs/baseline.yaml --repeats 3

Writes eval/results/<run_id>/:

- config.json: the effective config, its hash, seeds, and the command line.
- env.json: CodeRev Bot git SHA, dataset blob hash, Ollama version and
  model digest (Ollama provider only), OS, Python, CPU, start/end times.
- outputs.jsonl: one record per (case, repeat): config hash, case id,
  repeat, seed, findings, parse failures, every attempt's raw output and
  latency, failure type, truncation. No diffs or source files.

Scoring is not done here. eval/report.py reads these artifacts and applies
the matching function, so a run never has to be repeated to re-score it.

The reviewer is the production engine (reviewer/engine.py) with the
production prompt builder and parser, so the eval measures the shipped code.
"""

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx

from eval.cases import DEFAULT_CASES_PATH, CaseFileError, CaseSet, load_cases
from eval.config import ConfigError, RunConfig, load_config, seeds_for
from eval.fetch import DEFAULT_CACHE_DIR, FetchedCase, Fetcher, fetch_all
from reviewer import prompts
from reviewer.engine import EngineConfig, ReviewResult, run_review
from reviewer.providers import OllamaProvider, ReviewProvider, StubProvider

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"


def make_provider(cfg: RunConfig) -> ReviewProvider:
    if cfg.provider == "stub":
        return StubProvider(replies=list(cfg.stub_replies), model="stub", num_ctx=cfg.num_ctx)
    return OllamaProvider(
        base_url=cfg.base_url, model=cfg.model, timeout_s=cfg.timeout_s,
        num_ctx=cfg.num_ctx, temperature=cfg.temperature,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_run_id(cfg: RunConfig, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{cfg.name}-{cfg.config_hash()[:8]}"


def _git_sha() -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return {"coderev_git_sha": run("rev-parse", "HEAD") or None, "coderev_dirty": bool(run("status", "--porcelain"))}


def _ollama_info(cfg: RunConfig) -> dict:
    """Ollama version and model digest. Best effort: missing values stay None."""
    info = {"ollama_version": None, "model_digest": None}
    if cfg.provider != "ollama":
        return info
    try:
        with httpx.Client(timeout=5.0) as client:
            info["ollama_version"] = client.get(f"{cfg.base_url}/api/version").json().get("version")
            for model in client.get(f"{cfg.base_url}/api/tags").json().get("models", []):
                if model.get("name") == cfg.model or model.get("model") == cfg.model:
                    info["model_digest"] = model.get("digest")
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return info


def environment(cfg: RunConfig, case_set: CaseSet) -> dict:
    return {
        **_git_sha(),
        "dataset_path": case_set.path,
        "dataset_blob_hash": case_set.blob_hash,
        **_ollama_info(cfg),
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "ram_bytes": None,  # not portable via the stdlib; fill in by hand if needed
    }


def _finding_dict(finding) -> dict:
    return finding.to_dict()


def record_for(cfg: RunConfig, run_id: str, case, fetched: FetchedCase, repeat: int, seed: int,
               result: ReviewResult) -> dict:
    failure = result.failure
    return {
        "run_id": run_id,
        "config_hash": cfg.config_hash(),
        "case_id": case.id,
        "split": case.split,
        "repeat": repeat,
        "seed": seed,
        "pre_cutoff": fetched.pre_cutoff,
        "status": "ok" if failure is None else failure.kind,
        "failure": None if failure is None else {"kind": failure.kind, "detail": failure.detail},
        "findings": [_finding_dict(f) for f in result.findings],
        "parse_failures": [p.to_dict() for p in result.parse_failures],
        "retries": result.retries,
        "latency_s": result.latency_s,
        "truncated": result.truncated,
        "plan": result.plan.to_dict(),
        "diff_stats": {
            "files": len(fetched.files),
            "added_lines": fetched.added_lines,
            "removed_lines": fetched.removed_lines,
        },
        "chunks": [
            {
                "path": chunk.path,
                "attempts": [
                    {
                        "seed": a.seed,
                        "raw_text": a.raw_text,
                        "parse_status": a.parse_status,
                        "latency_s": a.latency_s,
                        "prompt_eval_count": a.prompt_eval_count,
                        "eval_count": a.eval_count,
                        "truncation_suspected": a.truncation_suspected,
                        "failure": None if a.failure is None else {"kind": a.failure.kind, "detail": a.failure.detail},
                    }
                    for a in chunk.attempts
                ],
            }
            for chunk in result.chunks
        ],
    }


def _done_keys(outputs_path: Path) -> set:
    done = set()
    if outputs_path.is_file():
        for line in outputs_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done.add((rec["case_id"], rec["repeat"]))
    return done


async def run_config(
    cfg: RunConfig,
    case_set: CaseSet,
    fetched: dict,
    out_dir: Path,
    run_id: str,
    seeds: tuple,
    provider: Optional[ReviewProvider] = None,
    progress: Callable[[str], None] = lambda msg: None,
    command: Optional[list] = None,
) -> Path:
    """Run every case for every seed and append records to outputs.jsonl."""
    out_dir.mkdir(parents=True, exist_ok=True)
    provider = provider or make_provider(cfg)
    outputs = out_dir / "outputs.jsonl"
    done = _done_keys(outputs)
    started = utc_now()

    config_doc = {
        "run_id": run_id,
        "config": cfg.to_dict(),
        "config_hash": cfg.config_hash(),
        "seeds": list(seeds),
        "engine": {"max_retries": cfg.effective_max_retries, "chunking": cfg.chunking, "num_ctx": cfg.num_ctx},
        "command": command if command is not None else sys.argv,
        "case_ids": [c.id for c in case_set.cases],
    }
    (out_dir / "config.json").write_text(json.dumps(config_doc, indent=2), encoding="utf-8")

    if cfg.warmup and cfg.provider != "stub":
        # Discarded warm-up so model load time is not charged to case 1 (§6.8).
        try:
            await provider.generate("Reply with NO_ISSUES_FOUND.", seed=0)
        except Exception as exc:  # noqa: BLE001  (the run itself will record failures)
            progress(f"warm-up failed: {exc}")

    build = lambda code: prompts.build_prompt(cfg.prompt_version, code)  # noqa: E731
    total = len(seeds) * len(case_set.cases)
    count = 0
    with outputs.open("a", encoding="utf-8") as sink:
        for repeat, seed in enumerate(seeds, start=1):
            engine_cfg = EngineConfig(chunking=cfg.chunking, seed=seed, max_retries=cfg.effective_max_retries)
            for case in case_set.cases:
                count += 1
                if (case.id, repeat) in done:
                    continue
                result = await run_review(fetched[case.id].diff, provider, build, engine_cfg)
                record = record_for(cfg, run_id, case, fetched[case.id], repeat, seed, result)
                sink.write(json.dumps(record) + "\n")
                sink.flush()
                progress(f"[{count}/{total}] {case.id} repeat {repeat}: {record['status']}, "
                         f"{len(record['findings'])} finding(s), {record['latency_s']:.1f}s")

    env = environment(cfg, case_set)
    env.update({"started_at": started, "finished_at": utc_now()})
    (out_dir / "env.json").write_text(json.dumps(env, indent=2), encoding="utf-8")
    return out_dir


def prepare(cfg: RunConfig, cases_path, cache_dir, offline: bool, limit: Optional[int] = None):
    """Load and fetch every case. Any problem fails the run before a model call."""
    case_set = load_cases(cases_path)
    if limit is not None:
        case_set = CaseSet(case_set.path, case_set.cases[:limit], case_set.categories, case_set.blob_hash)
    fetcher = Fetcher(cache_dir=cache_dir, offline=offline, context_lines=cfg.context_lines,
                      include_tests=cfg.include_tests)
    fetched, errors = fetch_all(case_set, fetcher, cfg.cutoff)
    return case_set, fetched, errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one eval configuration.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--repeats", type=int, default=None, help="use seeds 1..N instead of the config's seeds")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--offline", action="store_true", help="use cached diffs only")
    parser.add_argument("--resume", metavar="RUN_ID", help="continue an interrupted run")
    parser.add_argument("--pilot", type=int, metavar="N",
                        help="pilot on the first N cases; results go to a pilot- run id and must not be reported")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        seeds = seeds_for(cfg, args.repeats)
        case_set, fetched, errors = prepare(cfg, args.cases, Path(args.cache_dir), args.offline, args.pilot)
    except (ConfigError, CaseFileError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if errors:
        for case_id, message in sorted(errors.items()):
            print(f"ERROR {case_id}: {message}", file=sys.stderr)
        print(f"{len(errors)} case(s) failed validation or fetch; nothing was run.", file=sys.stderr)
        return 1

    run_id = args.resume or make_run_id(cfg)
    if args.pilot and not run_id.startswith("pilot-"):
        run_id = f"pilot-{run_id}"
    out_dir = Path(args.results_dir) / run_id
    started = time.perf_counter()
    asyncio.run(run_config(cfg, case_set, fetched, out_dir, run_id, seeds,
                           progress=lambda m: print(m, flush=True)))
    print(f"run {run_id} finished in {time.perf_counter() - started:.0f}s -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
