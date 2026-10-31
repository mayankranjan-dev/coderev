from pathlib import Path

import pytest
import yaml

from eval import ablation
from eval.ablation import MatrixError, load_matrix, plan

ROOT = Path(__file__).resolve().parent.parent


def write_matrix(tmp_path, variants, tolerance=3, **extra):
    (tmp_path / "base.yaml").write_text("name: baseline\n", encoding="utf-8")
    data = {"baseline": "base.yaml", "repeats": 2, "tolerance": tolerance, "variants": variants, **extra}
    path = tmp_path / "matrix.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_shipped_matrix_is_one_factor_at_a_time():
    matrix = load_matrix(ROOT / "eval" / "ablation.yaml")
    base = matrix["baseline"]
    names = [n for n, _ in matrix["variants"]]
    assert names == ["model-0.5b", "model-3b", "model-7b", "prompt-v2", "chunking-per-file", "retry-off"]
    for _, cfg in matrix["variants"]:
        diff = [f for f in ablation.FACTORS if getattr(cfg, f) != getattr(base, f)]
        assert len(diff) == 1
        assert cfg.seeds == base.seeds


@pytest.mark.parametrize(
    "variants,message",
    [
        ([{"name": "two", "set": {"model": "m", "chunking": "per_file"}}], "exactly one factor"),
        ([{"name": "none", "set": {"model": "qwen2.5-coder:1.5b"}}], "exactly one factor"),
        ([{"name": "seed", "set": {"seeds": [9]}}], "non-factor"),
        ([{"name": "a", "set": {"retry": False}}, {"name": "a", "set": {"chunking": "per_file"}}], "duplicate"),
        ([{"set": {"retry": False}}], "exactly 'name' and 'set'"),
    ],
)
def test_matrix_validation(tmp_path, variants, message):
    with pytest.raises(MatrixError, match=message):
        load_matrix(write_matrix(tmp_path, variants))


def test_tolerance_is_required(tmp_path):
    with pytest.raises(MatrixError, match="tolerance"):
        load_matrix(write_matrix(tmp_path, [], tolerance=None))


def test_missing_models_are_skipped_not_dropped(tmp_path):
    matrix = load_matrix(write_matrix(tmp_path, [{"name": "big", "set": {"model": "qwen2.5-coder:7b"}}]))
    planned = plan(matrix, None, False, lambda url: {"qwen2.5-coder:1.5b"})
    assert [(p.name, bool(p.skipped)) for p in planned] == [("baseline", False), ("big", True)]
    assert "not pulled" in planned[1].skipped


def test_only_keeps_baseline(tmp_path):
    matrix = load_matrix(write_matrix(tmp_path, [{"name": "r", "set": {"retry": False}},
                                                 {"name": "c", "set": {"chunking": "per_file"}}]))
    planned = plan(matrix, ["c"], True, lambda url: None)
    assert [p.name for p in planned] == ["baseline", "c"]
    assert all(p.config.provider == "stub" for p in planned)
    with pytest.raises(MatrixError):
        plan(matrix, ["nope"], True, lambda url: None)
