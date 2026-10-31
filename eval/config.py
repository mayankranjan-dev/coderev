"""
Run configuration for the eval harness (one point in the ablation space).

A config file is YAML (or JSON) with any of the fields below; omitted fields
take the baseline defaults from docs/EVAL_DESIGN.md §5.1. Unknown fields are
an error, so a typo cannot silently fall back to a default.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

import yaml

from reviewer import diffutil, prompts


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RunConfig:
    name: str = "baseline"
    provider: str = "ollama"  # ollama | stub
    model: str = "qwen2.5-coder:1.5b"
    prompt_version: str = "v1"
    chunking: str = diffutil.WHOLE
    retry: bool = True
    max_retries: int = 2  # extra attempts after unparseable output, when retry is on
    num_ctx: int = 8192
    temperature: float = 0.2
    # Long on purpose: slow reviews must finish so their latency is measured.
    timeout_s: float = 600.0
    base_url: str = "http://localhost:11434"
    seeds: tuple = (1, 2, 3)
    context_lines: int = 3
    include_tests: bool = False
    cutoff: str = "2025-01-01"
    warmup: bool = True
    # Only for provider: stub. Replies are cycled per call (see StubProvider).
    stub_replies: tuple = ("NO_ISSUES_FOUND",)
    # Production timeout, recorded so the report can count reviews production
    # would have marked llm_timeout (EVAL_DESIGN §4 "Over production timeout").
    production_timeout_s: float = 10.0

    @property
    def effective_max_retries(self) -> int:
        return self.max_retries if self.retry else 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["seeds"] = list(self.seeds)
        data["stub_replies"] = list(self.stub_replies)
        return data

    def config_hash(self) -> str:
        """Hash of everything that affects model behavior or scoring input."""
        data = self.to_dict()
        data.pop("name")  # renaming a config does not change what it measures
        if self.provider != "stub":
            data.pop("stub_replies")
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    def with_overrides(self, **overrides) -> "RunConfig":
        return from_dict({**self.to_dict(), **overrides})


_FIELD_NAMES = {f.name for f in fields(RunConfig)}


def _validate(cfg: RunConfig) -> RunConfig:
    problems = []
    if cfg.provider not in ("ollama", "stub"):
        problems.append(f"provider must be 'ollama' or 'stub', got {cfg.provider!r}")
    if cfg.chunking not in diffutil.STRATEGIES:
        problems.append(f"chunking must be one of {diffutil.STRATEGIES}, got {cfg.chunking!r}")
    try:
        prompts.load_template(cfg.prompt_version)
    except prompts.PromptError as exc:
        problems.append(str(exc))
    if not cfg.seeds or not all(isinstance(s, int) and not isinstance(s, bool) for s in cfg.seeds):
        problems.append("seeds must be a non-empty list of integers")
    elif len(set(cfg.seeds)) != len(cfg.seeds):
        problems.append("seeds must be distinct")
    if cfg.num_ctx <= 0:
        problems.append("num_ctx must be positive")
    if cfg.max_retries < 0:
        problems.append("max_retries must be >= 0")
    if cfg.timeout_s <= 0:
        problems.append("timeout_s must be positive")
    if problems:
        raise ConfigError("; ".join(problems))
    return cfg


def from_dict(data: dict) -> RunConfig:
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")
    unknown = sorted(set(data) - _FIELD_NAMES)
    if unknown:
        raise ConfigError(f"unknown config field(s): {', '.join(unknown)}")
    values = dict(data)
    for key in ("seeds", "stub_replies"):
        if key in values and isinstance(values[key], list):
            values[key] = tuple(values[key])
    try:
        cfg = RunConfig(**values)
    except TypeError as exc:
        raise ConfigError(str(exc)) from None
    return _validate(cfg)


def load_config(path) -> RunConfig:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    return from_dict(data)


def seeds_for(cfg: RunConfig, repeats: Optional[int]) -> tuple:
    """--repeats N overrides the config's seed list with seeds 1..N."""
    if repeats is None:
        return tuple(cfg.seeds)
    if repeats < 1:
        raise ConfigError("--repeats must be >= 1")
    return tuple(range(1, repeats + 1))


__all__ = ["ConfigError", "RunConfig", "from_dict", "load_config", "seeds_for", "field"]
