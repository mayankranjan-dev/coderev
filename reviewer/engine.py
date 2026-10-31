"""
Review engine: chunk the input, call the provider per chunk, parse each
output, and merge the results.

Used by the production paths (main.py) and the eval harness, so both measure
the same code.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from reviewer import diffutil
from reviewer.findings import Finding, ParseFailure, parse_review
from reviewer.providers import ProviderError, ReviewProvider

PromptBuilder = Callable[[str], str]


@dataclass
class EngineConfig:
    chunking: str = diffutil.WHOLE
    output_reserve_tokens: int = diffutil.DEFAULT_OUTPUT_RESERVE_TOKENS
    # Explicit budget override (characters of code per request). None means
    # derive it from the provider's num_ctx.
    budget_chars: Optional[int] = None
    seed: Optional[int] = None


@dataclass
class ChunkOutcome:
    path: Optional[str]
    raw_text: Optional[str] = None
    findings: list = field(default_factory=list)
    parse_failures: list = field(default_factory=list)
    parse_status: Optional[str] = None
    latency_s: Optional[float] = None
    prompt_eval_count: Optional[int] = None
    eval_count: Optional[int] = None
    truncation_suspected: bool = False
    error_kind: Optional[str] = None
    error_detail: Optional[str] = None


@dataclass
class ReviewResult:
    plan: diffutil.ChunkPlan
    chunks: list = field(default_factory=list)

    @property
    def findings(self) -> list:
        return [f for c in self.chunks for f in c.findings]

    @property
    def parse_failures(self) -> list:
        return [p for c in self.chunks for p in c.parse_failures]

    @property
    def provider_error(self) -> Optional[ChunkOutcome]:
        return next((c for c in self.chunks if c.error_kind), None)

    @property
    def raw_text(self) -> str:
        return "\n".join(c.raw_text for c in self.chunks if c.raw_text)

    @property
    def truncated(self) -> bool:
        """Input was cut to fit the budget, or Ollama's context window filled up."""
        return self.plan.truncated or any(c.truncation_suspected for c in self.chunks)

    @property
    def latency_s(self) -> float:
        return sum(c.latency_s or 0.0 for c in self.chunks)


def budget_for(provider: ReviewProvider, build_prompt: PromptBuilder, config: EngineConfig) -> int:
    if config.budget_chars is not None:
        return config.budget_chars
    num_ctx = provider.num_ctx or 8192
    template_chars = len(build_prompt(""))
    return max(1, diffutil.char_budget(num_ctx, template_chars, config.output_reserve_tokens))


async def run_review(
    code_text: str,
    provider: ReviewProvider,
    build_prompt: PromptBuilder,
    config: Optional[EngineConfig] = None,
) -> ReviewResult:
    """Never raises for provider or parse problems; they are recorded."""
    config = config or EngineConfig()
    plan = diffutil.plan_chunks(code_text, config.chunking, budget_for(provider, build_prompt, config))
    result = ReviewResult(plan=plan)

    for chunk in plan.chunks:
        outcome = ChunkOutcome(path=chunk.path)
        result.chunks.append(outcome)
        try:
            response = await provider.generate(build_prompt(chunk.text), seed=config.seed)
        except ProviderError as exc:
            outcome.error_kind, outcome.error_detail = exc.kind, exc.detail
            break  # the remaining chunks would hit the same failure
        parsed = parse_review(response.text)
        outcome.raw_text = response.text
        outcome.findings = parsed.findings
        outcome.parse_failures = parsed.failures
        outcome.parse_status = parsed.status
        outcome.latency_s = response.latency_s
        outcome.prompt_eval_count = response.prompt_eval_count
        outcome.eval_count = response.eval_count
        outcome.truncation_suspected = response.truncation_suspected
    return result


def counts(findings: list) -> dict:
    out = {"critical_count": 0, "warning_count": 0, "nitpick_count": 0}
    for finding in findings:
        key = f"{finding.severity.lower()}_count"
        if key in out:
            out[key] += 1
    return out


__all__ = ["EngineConfig", "ReviewResult", "ChunkOutcome", "run_review", "counts", "Finding", "ParseFailure"]
