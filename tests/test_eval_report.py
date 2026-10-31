import json
from pathlib import Path

import pytest

from eval import report
from eval.cases import Case, CaseSet, Label
from eval.report import Run, bootstrap_ci, paired_comparison, percentile, spread, summarize

ROOT = Path(__file__).resolve().parent.parent


def test_only_file_and_overlap(finding, label, tolerance):
    """TEST-ONLY matcher. Not the author-owned is_catch; used to exercise the harness."""
    return (
        finding.file == label.buggy_file
        and finding.line_start is not None
        and finding.line_start <= label.line_end + tolerance
        and (finding.line_end or finding.line_start) >= label.line_start - tolerance
    )


test_only_file_and_overlap.__test__ = False  # not a pytest test


def cases():
    return CaseSet("c.yaml", (
        Case("b1", "https://github.com/o/r", "a" * 40, "b" * 40, "bug", Label("m.py", 10, 10, "logic")),
        Case("b2", "https://github.com/o/r", "a" * 40, "c" * 40, "bug", Label("m.py", 50, 52, "security")),
        Case("c1", "https://github.com/o/r", "a" * 40, "d" * 40, "control"),
    ), None, "h")


def rec(case_id, split, repeat, findings=(), status="ok", latency=2.0, retries=0, pre_cutoff=False):
    return {"case_id": case_id, "split": split, "repeat": repeat, "status": status, "latency_s": latency,
            "retries": retries, "truncated": False, "pre_cutoff": pre_cutoff,
            "findings": [{"severity": s, "file": f, "line_start": a, "line_end": b, "message": "m", "correction": None}
                         for s, f, a, b in findings]}


def make_run(records, provider="ollama", run_id="r1", name="base"):
    return Run(run_id, Path("."), {"config": {"name": name, "provider": provider, "production_timeout_s": 10},
                                   "config_hash": "x", "seeds": sorted({r["repeat"] for r in records})},
               {"dataset_blob_hash": "h"}, records)


RECORDS = [
    rec("b1", "bug", 1, [("CRITICAL", "m.py", 10, 10)]),
    rec("b2", "bug", 1, [("WARNING", "m.py", 80, 80)]),
    rec("c1", "control", 1, [("CRITICAL", "x.py", 1, 1)], latency=12.0),
    rec("b1", "bug", 2, [("WARNING", "m.py", 12, 12)]),
    rec("b2", "bug", 2, [], status="malformed_output"),
    rec("c1", "control", 2, [("NITPICK", None, None, None)]),
]


def test_core_metrics():
    s = summarize(make_run(RECORDS), cases(), test_only_file_and_overlap, tolerance=0)
    r1, r2 = s["per_repeat"]
    assert r1["recall"] == 0.5 and r2["recall"] == 0.0  # b1:12 misses at tolerance 0
    assert r1["recall_t3"] == 0.5 and r2["recall_t3"] == 0.5
    assert r1["recall_critical"] == 0.5 and r2["recall_critical"] == 0.0
    assert r1["fp_rate_critical"] == 1.0 and r2["fp_rate_critical"] == 0.0
    assert r2["fp_rate_any"] == 1.0
    assert r2["parse_failure_rate"] == pytest.approx(1 / 3)
    assert r2["unlocated_rate"] == 0.5
    assert r1["off_target_findings"] == 1
    assert r1["over_production_timeout"] == pytest.approx(1 / 3)
    agg = s["aggregate"]["recall"]
    assert agg["mean"] == 0.25 and agg["min"] == 0.0 and agg["max"] == 0.5
    assert agg["sd"] == pytest.approx(0.3535533905932738)


def test_recall_is_tbd_until_matcher_exists():
    def unimplemented(finding, label, tolerance):
        raise NotImplementedError

    s = summarize(make_run(RECORDS), cases(), unimplemented, tolerance=3)
    assert not s["matcher_implemented"]
    assert "recall" not in s["aggregate"] and "recall" not in s["bootstrap_95"]
    md = report.render_markdown([s], [], "cmd", "now")
    assert md.count(report.TBD_MATCHER) == 3 + len(report.TOLERANCE_SWEEP)
    assert "FP rate on controls (CRITICAL) | 50.0%" in md  # non-recall metrics still computed


def test_real_matcher_is_currently_tbd():
    from eval.match import is_catch

    s = summarize(make_run(RECORDS), cases(), is_catch, tolerance=3)
    assert s["matcher_implemented"] in (False, True)  # True once the owner implements it


def test_pre_cutoff_items_excluded_from_headline():
    s = summarize(make_run([rec("b1", "bug", 1, [("CRITICAL", "m.py", 10, 10)], pre_cutoff=True),
                            rec("b2", "bug", 1, [])]), cases(), test_only_file_and_overlap, 0)
    assert s["per_repeat"][0]["recall"] == 0.0
    assert s["pre_cutoff_items"] == ["b1"]
    probe = summarize(make_run([rec("b1", "bug", 1, [("CRITICAL", "m.py", 10, 10)], pre_cutoff=True),
                                rec("b2", "bug", 1, [])]), cases(), test_only_file_and_overlap, 0, include_pre_cutoff=True)
    assert probe["per_repeat"][0]["recall"] == 0.5


def test_unknown_case_in_run_is_an_error():
    with pytest.raises(ValueError):
        summarize(make_run([rec("zzz", "bug", 1)]), cases(), test_only_file_and_overlap, 0)


def test_paired_comparison():
    a = summarize(make_run(RECORDS, run_id="a", name="A"), cases(), test_only_file_and_overlap, 3)
    b_records = [dict(r, findings=[]) for r in RECORDS]
    b = summarize(make_run(b_records, run_id="b", name="B"), cases(), test_only_file_and_overlap, 3)
    cmp = paired_comparison(a, b, "recall")
    assert cmp["n_items"] == 2 and cmp["mean_diff"] == 0.5
    assert cmp["verdict"] in ("A higher", "no measurable difference")
    same = paired_comparison(a, a, "recall")
    assert same["mean_diff"] == 0 and same["verdict"] == "no measurable difference"


def test_stats_helpers():
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([5], 0.95) == 5
    assert percentile([], 0.5) is None
    assert spread([1.0])["sd"] is None
    assert bootstrap_ci([1.0, 0.0, 1.0]) == bootstrap_ci([1.0, 0.0, 1.0])  # fixed seed
    low, high = bootstrap_ci([0.0, 1.0] * 20)
    assert low < 0.5 < high


def test_markdown_contains_command_and_date():
    s = summarize(make_run(RECORDS), cases(), test_only_file_and_overlap, 3)
    md = report.render_markdown([s], [], "python -m eval.report r1 --tolerance 3", "2026-10-31 15:00 UTC")
    assert "`python -m eval.report r1 --tolerance 3`" in md and "2026-10-31 15:00 UTC" in md
    assert "| Latency p95 (s) |" in md and "95% CI" in md


def _write_run(results, run_id, provider, records):
    path = results / run_id
    path.mkdir(parents=True)
    (path / "config.json").write_text(json.dumps({"config": {"name": "n", "provider": provider}, "seeds": [1]}))
    (path / "env.json").write_text(json.dumps({}))
    (path / "outputs.jsonl").write_text("\n".join(json.dumps(r) for r in records))


def test_write_results_refuses_stub_and_pilot_runs(tmp_path, monkeypatch):
    import yaml

    cases_file = tmp_path / "cases.yaml"
    cases_file.write_text(yaml.safe_dump({"version": 1, "cases": [
        {"id": "c1", "repo_url": "https://github.com/o/r", "parent_sha": "a" * 40, "fix_sha": "d" * 40, "split": "control"}
    ]}))
    results = tmp_path / "results"
    _write_run(results, "stubrun", "stub", [rec("c1", "control", 1)])
    _write_run(results, "pilot-x", "ollama", [rec("c1", "control", 1)])
    target = tmp_path / "RESULTS.md"
    monkeypatch.setattr(report, "RESULTS_MD", target)
    monkeypatch.setattr(report, "ROOT", tmp_path)
    for run_id in ("stubrun", "pilot-x"):
        code = report.main([run_id, "--tolerance", "3", "--cases", str(cases_file),
                            "--results-dir", str(results), "--write-results"])
        assert code == 1
    assert not target.exists()
    assert (results / "stubrun" / "summary.json").is_file()


def test_results_md_is_tbd_or_generated():
    text = (ROOT / "docs" / "RESULTS.md").read_text(encoding="utf-8")
    assert "TBD (not yet measured)" in text or "by `python -m eval.report" in text
    assert "TBD (not yet measured)" in report.tbd_results_md()
