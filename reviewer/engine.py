"""
Review engine: chunk the input, call the provider per chunk, parse each
output, retry unusable output a bounded number of times, and merge the
results into one typed ReviewResult.

Used by the production paths (main.py) and the eval harness, so both measure
the same code.

Failure handling. run_review() never raises. Every outcome is data:

- Transport failures (unreachable, timeout, provider_error) come from the
  provider. They are not retried here: the same request would most likely
  fail again right away. Production retries them later via retry_queue.py.
- Unusable output (malformed_output: text with neither a finding nor the
  NO_ISSUES_FOUND sentinel; empty_output: blank text) is retried up to
  max_retries extra times, with the next seed when a seed is set. Only the
  last attempt's output counts; every attempt is kept in `attempts`.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from reviewer import diffutil, findings as findings_mod, providers
from reviewer.findings import Finding, ParseFailure, parse_review
from reviewer.providers import ProviderError, ReviewProvider

PromptBuilder = Callable[[str], str]

# Failure.kind values
UNREACHABLE = "unreachable"
TIMEOUT = "timeout"
PROVIDER_ERROR = "provider_error"
MALFORMED_OUTPUT = "malformed_output"
EMPTY_OUTPUT = "empty_output"
TRANSPORT_KINDS = (UNREACHABLE, TIMEOUT, PROVIDER_ERROR)
OUTPUT_KINDS = (MALFORMED_OUTPUT, EMPTY_OUTPUT)

_PROVIDER_KIND = {
    providers.UNREACHABLE: UNREACHABLE,
    providers.TIMEOUT: TIMEOUT,
    providers.HTTP_ERROR: PROVIDER_ERROR,
    providers.BAD_RESPONSE: PROVIDER_ERROR,
}
_PARSE_KIND = {findings_mod.MALFORMED: MALFORMED_OUTPUT, findings_mod.EMPTY: EMPTY_OUTPUT}


@dataclass
class Failure:
    kind: str
    detail: str
    chunk_path: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


@dataclass
class EngineConfig:
    chunking: str = diffutil.WHOLE
    output_reserve_tokens: int = diffutil.DEFAULT_OUTPUT_RESERVE_TOKENS
    # Explicit budget override (characters of code per request). None means
    # derive it from the provider's num_ctx.
    budget_chars: Optional[int] = None
    seed: Optional[int] = None
    # Extra attempts after malformed or empty output. 0 disables retry.
    max_retries: int = 1


@dataclass
class Attempt:
    seed: Optional[int]
    raw_text: Optional[str] = None
    parse_status: Optional[str] = None
    latency_s: Optional[float] = None
    prompt_eval_count: Optional[int] = None
    eval_count: Optional[int] = None
    truncation_suspected: bool = False
    failure: Optional[Failure] = None


@dataclass
class ChunkOutcome:
    path: Optional[str]
    attempts: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    parse_failures: list = field(default_factory=list)

    @property
    def final(self) -> Optional[Attempt]:
        return self.attempts[-1] if self.attempts else None

    @property
    def failure(self) -> Optional[Failure]:
        return self.final.failure if self.final else None

    @property
    def raw_text(self) -> Optional[str]:
        return self.final.raw_text if self.final else None

    @property
    def retries(self) -> int:
        return max(0, len(self.attempts) - 1)

    @property
    def latency_s(self) -> float:
        return sum(a.latency_s or 0.0 for a in self.attempts)


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
    def failure(self) -> Optional[Failure]:
        """
        The review-level failure, or None if the review produced usable output.
        A transport failure in any chunk fails the review (the input was not
        fully reviewed). Output failures fail it only if no chunk was usable.
        """
        chunk_failures = [c.failure for c in self.chunks if c.failure]
        for failure in chunk_failures:
            if failure.kind in TRANSPORT_KINDS:
                return failure
        if chunk_failures and len(chunk_failures) == len(self.chunks):
            malformed = [f for f in chunk_failures if f.kind == MALFORMED_OUTPUT]
            return malformed[0] if malformed else chunk_failures[0]
        return None

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def raw_text(self) -> str:
        return "\n".join(c.raw_text for c in self.chunks if c.raw_text)

    @property
    def truncated(self) -> bool:
        """Input was cut to fit the budget, or Ollama's context window filled up."""
        return self.plan.truncated or any(
            a.truncation_suspected for c in self.chunks for a in c.attempts
        )

    @property
    def latency_s(self) -> float:
        return sum(c.latency_s for c in self.chunks)

    @property
    def retries(self) -> int:
        return sum(c.retries for c in self.chunks)


def budget_for(provider: ReviewProvider, build_prompt: PromptBuilder, config: EngineConfig) -> int:
    if config.budget_chars is not None:
        return config.budget_chars
    num_ctx = provider.num_ctx or 8192
    template_chars = len(build_prompt(""))
    return max(1, diffutil.char_budget(num_ctx, template_chars, config.output_reserve_tokens))


def _seed_for(config: EngineConfig, attempt_index: int) -> Optional[int]:
    return None if config.seed is None else config.seed + attempt_index


async def _attempt(provider: ReviewProvider, prompt: str, seed: Optional[int], path) -> tuple[Attempt, object]:
    attempt = Attempt(seed=seed)
    try:
        response = await provider.generate(prompt, seed=seed)
    except ProviderError as exc:
        attempt.failure = Failure(_PROVIDER_KIND.get(exc.kind, PROVIDER_ERROR), exc.detail, path)
        return attempt, None
    except Exception as exc:  # noqa: BLE001  (contract: nothing escapes run_review)
        attempt.failure = Failure(PROVIDER_ERROR, f"unexpected {type(exc).__name__}", path)
        return attempt, None

    parsed = parse_review(response.text)
    attempt.raw_text = response.text
    attempt.parse_status = parsed.status
    attempt.latency_s = response.latency_s
    attempt.prompt_eval_count = response.prompt_eval_count
    attempt.eval_count = response.eval_count
    attempt.truncation_suspected = response.truncation_suspected
    if parsed.status in _PARSE_KIND:
        detail = (
            "model output was blank"
            if parsed.status == findings_mod.EMPTY
            else f"no finding or NO_ISSUES_FOUND line in {len(parsed.failures)} line(s)"
        )
        attempt.failure = Failure(_PARSE_KIND[parsed.status], detail, path)
    return attempt, parsed


async def run_review(
    code_text: str,
    provider: ReviewProvider,
    build_prompt: PromptBuilder,
    config: Optional[EngineConfig] = None,
) -> ReviewResult:
    """Never raises; provider, parse, and planning problems are recorded."""
    config = config or EngineConfig()
    try:
        plan = diffutil.plan_chunks(code_text, config.chunking, budget_for(provider, build_prompt, config))
    except Exception as exc:  # noqa: BLE001  (bad config must not escape either)
        plan = diffutil.ChunkPlan(strategy=config.chunking, budget_chars=0)
        result = ReviewResult(plan=plan)
        outcome = ChunkOutcome(path=None)
        outcome.attempts.append(Attempt(seed=None, failure=Failure(PROVIDER_ERROR, f"planning failed: {exc}")))
        result.chunks.append(outcome)
        return result

    result = ReviewResult(plan=plan)
    for chunk in plan.chunks:
        outcome = ChunkOutcome(path=chunk.path)
        result.chunks.append(outcome)
        try:
            prompt = build_prompt(chunk.text)
        except Exception as exc:  # noqa: BLE001
            outcome.attempts.append(Attempt(seed=None, failure=Failure(PROVIDER_ERROR, f"prompt build failed: {exc}")))
            break

        for attempt_index in range(1 + max(0, config.max_retries)):
            attempt, parsed = await _attempt(provider, prompt, _seed_for(config, attempt_index), chunk.path)
            outcome.attempts.append(attempt)
            if attempt.failure is None or attempt.failure.kind in TRANSPORT_KINDS:
                break
        final = outcome.final
        if final.failure is None and parsed is not None:
            outcome.findings = parsed.findings
        if parsed is not None:
            outcome.parse_failures = parsed.failures
        if final.failure is not None and final.failure.kind in TRANSPORT_KINDS:
            break  # the remaining chunks would hit the same failure
    return result


def counts(findings: list) -> dict:
    out = {"critical_count": 0, "warning_count": 0, "nitpick_count": 0}
    for finding in findings:
        key = f"{finding.severity.lower()}_count"
        if key in out:
            out[key] += 1
    return out


__all__ = [
    "Attempt", "ChunkOutcome", "EngineConfig", "Failure", "Finding", "ParseFailure",
    "ReviewResult", "counts", "run_review",
]
