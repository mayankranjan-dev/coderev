"""
Model providers for the review engine.

A provider turns a prompt into raw model text. It knows nothing about review
formats or severities; that lives in the parser. Every provider failure is
raised as ProviderError with a `kind`, so callers can map it to a status
without catching transport-specific exceptions.

- OllamaProvider: calls Ollama's /api/generate. num_ctx is always sent
  explicitly. Ollama's default context window varies by version and model,
  and a prompt longer than it is truncated without any error, so leaving it
  implicit would silently drop the end of large diffs.
- StubProvider: deterministic, in-process, for tests and harness dry runs.
"""

import abc
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Union

import httpx

# ProviderError.kind values.
UNREACHABLE = "unreachable"
TIMEOUT = "timeout"
HTTP_ERROR = "http_error"
BAD_RESPONSE = "bad_response"


class ProviderError(Exception):
    """A failure to get text out of the model at all (not a format problem)."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass
class ProviderResponse:
    text: str
    latency_s: float
    model: str
    prompt_eval_count: Optional[int] = None
    eval_count: Optional[int] = None
    num_ctx: Optional[int] = None

    @property
    def truncation_suspected(self) -> bool:
        """
        True when the prompt filled the context window, meaning Ollama likely
        dropped part of the input. Ollama reports the tokens it evaluated, not
        the tokens it discarded, so this is a heuristic, not proof.
        """
        return (
            self.prompt_eval_count is not None
            and self.num_ctx is not None
            and self.prompt_eval_count >= self.num_ctx
        )


class ReviewProvider(abc.ABC):
    name: str = "provider"
    model: str = ""
    num_ctx: Optional[int] = None

    @abc.abstractmethod
    async def generate(self, prompt: str, *, seed: Optional[int] = None) -> ProviderResponse:
        """Return the model's text for `prompt`. Raises ProviderError only."""

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Cheap reachability check. Never raises."""


class OllamaProvider(ReviewProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float,
        num_ctx: int,
        temperature: float = 0.2,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        if num_ctx <= 0:
            raise ValueError("num_ctx must be positive")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx
        self.temperature = temperature
        self._transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        kwargs = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def build_payload(self, prompt: str, seed: Optional[int]) -> dict:
        options = {"temperature": self.temperature, "num_ctx": self.num_ctx}
        if seed is not None:
            options["seed"] = seed
        return {"model": self.model, "prompt": prompt, "stream": False, "options": options}

    async def generate(self, prompt: str, *, seed: Optional[int] = None) -> ProviderResponse:
        payload = self.build_payload(prompt, seed)
        started = time.perf_counter()
        try:
            async with self._client(self.timeout_s) as client:
                response = await client.post(f"{self.base_url}/api/generate", json=payload)
                response.raise_for_status()
                body = response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderError(UNREACHABLE, type(exc).__name__) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError(TIMEOUT, f"no response within {self.timeout_s:g}s") from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError(HTTP_ERROR, f"HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(BAD_RESPONSE, type(exc).__name__) from exc
        except ValueError as exc:
            raise ProviderError(BAD_RESPONSE, "body is not JSON") from exc
        latency = time.perf_counter() - started

        if not isinstance(body, dict):
            raise ProviderError(BAD_RESPONSE, "body is not a JSON object")
        text = body.get("response")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ProviderError(BAD_RESPONSE, "'response' is not a string")

        return ProviderResponse(
            text=text,
            latency_s=latency,
            model=self.model,
            prompt_eval_count=_int_or_none(body.get("prompt_eval_count")),
            eval_count=_int_or_none(body.get("eval_count")),
            num_ctx=self.num_ctx,
        )

    async def is_available(self) -> bool:
        try:
            async with self._client(3.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                return response.status_code == 200
        except httpx.HTTPError:
            return False


def _int_or_none(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


StubReply = Union[str, ProviderError, Callable[[str, Optional[int]], Union[str, ProviderError]]]


@dataclass
class StubProvider(ReviewProvider):
    """
    Returns scripted replies in order (the last one repeats). A reply can be a
    string, a ProviderError to raise, or a function of (prompt, seed) that
    returns either. Every prompt and seed is recorded in `calls`.
    """

    replies: Sequence[StubReply] = ("NO_ISSUES_FOUND",)
    available: bool = True
    latency_s: float = 0.0
    model: str = "stub"
    num_ctx: Optional[int] = 8192
    prompt_eval_count: Optional[int] = None
    calls: list = field(default_factory=list)
    name: str = "stub"

    async def generate(self, prompt: str, *, seed: Optional[int] = None) -> ProviderResponse:
        index = min(len(self.calls), len(self.replies) - 1)
        self.calls.append((prompt, seed))
        reply = self.replies[index]
        if callable(reply) and not isinstance(reply, ProviderError):
            reply = reply(prompt, seed)
        if isinstance(reply, ProviderError):
            raise reply
        return ProviderResponse(
            text=reply,
            latency_s=self.latency_s,
            model=self.model,
            prompt_eval_count=self.prompt_eval_count,
            eval_count=None,
            num_ctx=self.num_ctx,
        )

    async def is_available(self) -> bool:
        return self.available
