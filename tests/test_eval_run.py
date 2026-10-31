import asyncio
import json

import pytest

from eval.cases import Case, CaseSet, Label
from eval.config import ConfigError, RunConfig, from_dict, load_config, seeds_for
from eval.fetch import FetchedCase
from eval.run import make_run_id, run_config
from reviewer.providers import StubProvider

DIFF = "--- a/pkg/m.py\n+++ b/pkg/m.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n"


def case_set():
    cases = (
        Case("bug-1", "https://github.com/o/r", "a" * 40, "b" * 40, "bug", Label("pkg/m.py", 2, 2, "logic")),
        Case("ctl-1", "https://github.com/o/r", "c" * 40, "d" * 40, "control"),
    )
    return CaseSet("cases.yaml", cases, None, "f" * 40)


def fetched():
    return {
        cid: FetchedCase(cid, "reverse" if cid.startswith("bug") else "forward", 3, ["pkg/m.py"], DIFF,
                         "2025-06-01T00:00:00+00:00", False, 1, 1)
        for cid in ("bug-1", "ctl-1")
    }


def stub_cfg(**kw):
    return from_dict({"name": "t", "provider": "stub", "warmup": False, **kw})


def run(tmp_path, cfg, provider, seeds=(1, 2), run_id="rid"):
    out = tmp_path / run_id
    asyncio.run(run_config(cfg, case_set(), fetched(), out, run_id, seeds, provider=provider, command=["test"]))
    return out, [json.loads(line) for line in (out / "outputs.jsonl").read_text().splitlines()]


def test_one_record_per_case_and_repeat(tmp_path):
    stub = StubProvider(replies=["CRITICAL: pkg/m.py:2 y wrong Correction: z"])
    out, records = run(tmp_path, stub_cfg(), stub, seeds=(1, 2, 3))
    assert [(r["case_id"], r["repeat"], r["seed"]) for r in records] == [
        ("bug-1", 1, 1), ("ctl-1", 1, 1), ("bug-1", 2, 2), ("ctl-1", 2, 2), ("bug-1", 3, 3), ("ctl-1", 3, 3),
    ]
    rec = records[0]
    assert rec["status"] == "ok" and rec["split"] == "bug"
    assert rec["findings"][0]["file"] == "pkg/m.py"
    assert rec["config_hash"] == stub_cfg().config_hash()
    assert rec["chunks"][0]["attempts"][0]["raw_text"].startswith("CRITICAL")
    assert {"latency_s", "retries", "truncated", "failure", "parse_failures", "pre_cutoff"} <= set(rec)


def test_seeds_reach_the_provider(tmp_path):
    stub = StubProvider(replies=["NO_ISSUES_FOUND"])
    run(tmp_path, stub_cfg(), stub, seeds=(7, 9))
    assert [seed for _, seed in stub.calls] == [7, 7, 9, 9]


def test_outputs_never_contain_the_diff(tmp_path):
    out, _ = run(tmp_path, stub_cfg(), StubProvider(replies=["NO_ISSUES_FOUND"]))
    assert "y = 3" not in (out / "outputs.jsonl").read_text()


def test_config_and_env_artifacts(tmp_path):
    out, _ = run(tmp_path, stub_cfg(), StubProvider())
    config = json.loads((out / "config.json").read_text())
    assert config["seeds"] == [1, 2] and config["command"] == ["test"]
    assert config["config"]["prompt_version"] == "v1"
    env = json.loads((out / "env.json").read_text())
    assert env["dataset_blob_hash"] == "f" * 40
    assert env["started_at"] and env["finished_at"]


def test_failures_are_recorded_not_raised(tmp_path):
    from reviewer.providers import ProviderError

    stub = StubProvider(replies=[ProviderError("timeout", "slow")])
    _, records = run(tmp_path, stub_cfg(), stub, seeds=(1,))
    assert {r["status"] for r in records} == {"timeout"}


def test_retry_toggle(tmp_path):
    garbage = StubProvider(replies=["not a review"])
    _, on = run(tmp_path, stub_cfg(retry=True, max_retries=2), garbage, seeds=(1,), run_id="on")
    assert on[0]["retries"] == 2 and on[0]["status"] == "malformed_output"
    garbage2 = StubProvider(replies=["not a review"])
    _, off = run(tmp_path, stub_cfg(retry=False), garbage2, seeds=(1,), run_id="off")
    assert off[0]["retries"] == 0


def test_resume_skips_finished_pairs(tmp_path):
    stub = StubProvider(replies=["NO_ISSUES_FOUND"])
    run(tmp_path, stub_cfg(), stub, seeds=(1,))
    stub2 = StubProvider(replies=["NO_ISSUES_FOUND"])
    _, records = run(tmp_path, stub_cfg(), stub2, seeds=(1, 2))
    assert len(stub2.calls) == 2  # only repeat 2
    assert len(records) == 4


def test_config_hash_ignores_name_but_not_settings():
    assert stub_cfg(name="a").config_hash() == stub_cfg(name="b").config_hash()
    assert stub_cfg().config_hash() != stub_cfg(prompt_version="v2").config_hash()
    assert stub_cfg().config_hash() != stub_cfg(seeds=[4, 5, 6]).config_hash()


def test_run_id_format():
    from datetime import datetime, timezone

    rid = make_run_id(stub_cfg(name="base"), datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert rid.startswith("20260102T030405Z-base-") and len(rid.split("-")[-1]) == 8


@pytest.mark.parametrize(
    "data,message",
    [
        ({"modle": "x"}, "unknown config field"),
        ({"prompt_version": "v99"}, "unknown prompt version"),
        ({"chunking": "diagonal"}, "chunking must be one of"),
        ({"seeds": []}, "seeds must be a non-empty list"),
        ({"seeds": [1, 1]}, "seeds must be distinct"),
        ({"provider": "openai"}, "provider must be"),
        ({"num_ctx": 0}, "num_ctx must be positive"),
    ],
)
def test_config_validation(data, message):
    with pytest.raises(ConfigError, match=message):
        from_dict(data)


def test_baseline_config_file_loads():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parent.parent / "eval" / "configs" / "baseline.yaml")
    assert cfg == RunConfig()  # the file states the defaults explicitly


def test_repeats_override_seeds():
    assert seeds_for(RunConfig(), 5) == (1, 2, 3, 4, 5)
    assert seeds_for(RunConfig(), None) == (1, 2, 3)
    with pytest.raises(ConfigError):
        seeds_for(RunConfig(), 0)
