"""
End-to-end harness tests: 6 synthetic cases, a pre-filled diff cache (so the
fetcher runs offline with no git or network), the stub provider, and a
TEST-ONLY matcher. The author-owned eval.match.is_catch is not used here, so
these pass whether or not it is implemented.
"""

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from eval import ablation, report
from eval.cases import load_cases
from eval.config import from_dict
from eval.fetch import FetchedCase, Fetcher
from eval.run import prepare, run_config
from reviewer.providers import StubProvider


def test_only_matcher(finding, label, tolerance):
    """TEST-ONLY: exact path and range overlap with tolerance. Not the real rule."""
    if finding.file != label.buggy_file or finding.line_start is None:
        return False
    end = finding.line_end if finding.line_end is not None else finding.line_start
    return finding.line_start <= label.line_end + tolerance and end >= label.line_start - tolerance


test_only_matcher.__test__ = False

# Synthetic cases. SHAs, repo, and code are made up for the test.
CASES = [
    ("syn-bug-1", "bug", "pkg/alpha.py", 10, 11),
    ("syn-bug-2", "bug", "pkg/beta.py", 3, 3),
    ("syn-bug-3", "bug", "pkg/gamma.py", 20, 22),
    ("syn-bug-4", "bug", "pkg/delta.py", 7, 7),
    ("syn-ctl-1", "control", "pkg/eps.py", None, None),
    ("syn-ctl-2", "control", "pkg/zeta.py", None, None),
]


def _diff(path, start):
    return f"--- a/{path}\n+++ b/{path}\n@@ -{start},2 +{start},2 @@\n x = 0\n-y = 1\n+y = 2\n"


@pytest.fixture
def workspace(tmp_path):
    entries = []
    for i, (cid, split, path, start, end) in enumerate(CASES):
        entry = {"id": cid, "repo_url": "https://github.com/example-org/synthetic",
                 "parent_sha": f"{i + 1:x}" * 40, "fix_sha": f"{i + 8:x}" * 40, "split": split}
        if split == "bug":
            entry.update(buggy_file=path, line_start=start, line_end=end, category="logic")
        entries.append(entry)
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(yaml.safe_dump({"version": 1, "cases": entries}, sort_keys=False), encoding="utf-8")

    cache = tmp_path / "cache"
    fetcher = Fetcher(cache_dir=cache, offline=True)
    for case, (_, split, path, start, _) in zip(load_cases(cases_path).cases, CASES):
        fc = FetchedCase(case.id, "reverse" if split == "bug" else "forward", 3, [path],
                         _diff(path, start or 1), "2025-06-01T00:00:00+00:00", False, 1, 1)
        target = fetcher._cache_path(case)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(fc.to_json()), encoding="utf-8")
    return {"cases": cases_path, "cache": cache, "results": tmp_path / "results"}


def label_reply(prompt, seed, path_override=None, lines=None):
    """Stub reply that points at the labeled lines of whichever case is in the prompt."""
    for cid, split, path, start, end in CASES:
        if f"+++ b/{path}" in prompt:
            if split == "control":
                return "NO_ISSUES_FOUND"
            a, b = lines or (start, end)
            return f"CRITICAL: {path_override or path}:{a}-{b} defect Correction: fix"
    return "NO_ISSUES_FOUND"


def run_stub(ws, reply, run_id, seeds=(1, 2, 3), **cfg):
    config = from_dict({"name": run_id, "provider": "stub", "warmup": False, **cfg})
    case_set, fetched, errors = prepare(config, ws["cases"], ws["cache"], offline=True)
    assert not errors, errors
    asyncio.run(run_config(config, case_set, fetched, ws["results"] / run_id, run_id, seeds,
                           provider=StubProvider(replies=[reply]), command=["test"]))
    return report.load_run(ws["results"], run_id), case_set


def summary(ws, reply, run_id, tolerance=0, **cfg):
    run, case_set = run_stub(ws, reply, run_id, **cfg)
    return report.summarize(run, case_set, test_only_matcher, tolerance)


def test_offline_pipeline_uses_cache_only(workspace):
    config = from_dict({"provider": "stub"})
    case_set, fetched, errors = prepare(config, workspace["cases"], workspace["cache"], offline=True)
    assert not errors and len(fetched) == 6 and all(f.from_cache for f in fetched.values())


def test_perfect_stub_scores_full_recall_and_zero_fp(workspace):
    s = summary(workspace, label_reply, "perfect")
    assert s["aggregate"]["recall"]["mean"] == 1.0
    assert s["aggregate"]["fp_rate_critical"]["mean"] == 0.0
    assert s["aggregate"]["parse_failure_rate"]["mean"] == 0.0


def test_flooding_cannot_inflate_recall(workspace):
    # 50 copies of a correct finding count as one catch per item: recall is
    # item-level and capped at 1.0, so repeating a hit gains nothing.
    def flood_hits(prompt, seed):
        return "\n".join([label_reply(prompt, seed)] * 50)

    one = summary(workspace, label_reply, "one-hit")
    many = summary(workspace, flood_hits, "many-hits")
    assert many["aggregate"]["recall"]["mean"] == one["aggregate"]["recall"]["mean"] == 1.0

    # 50 findings on lines far from every label: no recall, and the flood
    # shows up as off-target findings and as false positives on controls.
    def flood_misses(prompt, seed):
        path = next((p for _, _, p, _, _ in CASES if f"+++ b/{p}" in prompt), "x.py")
        return "\n".join(f"CRITICAL: {path}:{900 + i} noise Correction: n" for i in range(50))

    noisy = summary(workspace, flood_misses, "flood", tolerance=10)
    agg = noisy["aggregate"]
    assert agg["recall"]["mean"] == 0.0
    assert agg["fp_rate_critical"]["mean"] == 1.0
    assert agg["off_target_findings"]["mean"] == 4 * 50
    assert agg["findings_per_review_bug"]["mean"] == 50


def test_wrong_file_never_counts(workspace):
    def wrong_file(prompt, seed):
        return label_reply(prompt, seed, path_override="pkg/not_the_file.py")

    run, case_set = run_stub(workspace, wrong_file, "wrong-file")
    for tolerance in (0, 3, 5, 10, 1000):
        s = report.summarize(run, case_set, test_only_matcher, tolerance)
        assert s["aggregate"]["recall"]["mean"] == 0.0, tolerance
        assert all(s["aggregate"][f"recall_t{t}"]["mean"] == 0.0 for t in report.TOLERANCE_SWEEP)


def test_unlocated_findings_never_count(workspace):
    s = summary(workspace, lambda p, s: "CRITICAL: something is wrong Correction: fix it", "unlocated", tolerance=1000)
    assert s["aggregate"]["recall"]["mean"] == 0.0
    assert s["aggregate"]["unlocated_rate"]["mean"] == 1.0


def _stable(records):
    return [{k: v for k, v in r.items() if k != "run_id"} for r in records]


def test_stub_run_is_reproducible(workspace):
    def seed_dependent(prompt, seed):
        # Hit on odd seeds, miss on even ones: output depends on the seed only.
        return label_reply(prompt, seed) if seed % 2 else "NO_ISSUES_FOUND"

    a, _ = run_stub(workspace, seed_dependent, "repro-a")
    b, _ = run_stub(workspace, seed_dependent, "repro-b")
    assert _stable(a.records) == _stable(b.records)
    case_set = load_cases(workspace["cases"])
    sa = report.summarize(a, case_set, test_only_matcher, 0)
    sb = report.summarize(b, case_set, test_only_matcher, 0)
    assert sa["aggregate"] == sb["aggregate"]
    assert sa["bootstrap_95"] == sb["bootstrap_95"]
    assert [m["recall"] for m in sa["per_repeat"]] == [1.0, 0.0, 1.0]


def test_ablation_dry_run_end_to_end(workspace, tmp_path):
    base = tmp_path / "base.yaml"
    base.write_text("name: baseline\n", encoding="utf-8")
    matrix_path = tmp_path / "matrix.yaml"
    matrix_path.write_text(yaml.safe_dump({
        "baseline": "base.yaml", "repeats": 2, "tolerance": 0,
        "variants": [{"name": "retry-off", "set": {"retry": False}}],
    }), encoding="utf-8")
    matrix = ablation.load_matrix(matrix_path)
    planned = ablation.plan(matrix, None, True, lambda url: None)
    ablation.run_matrix(matrix, planned, workspace["cases"], workspace["cache"], workspace["results"], True,
                        provider_for=lambda cfg: StubProvider(replies=[label_reply]), progress=lambda m: None)
    assert all(p.run_id.startswith("dryrun-") for p in planned)
    table = ablation.combined_table(planned, workspace["cases"], workspace["results"], 0, "test", matcher=test_only_matcher)
    assert "| Recall | 100.0%" in table
    assert "retry-off | baseline | recall" in table
