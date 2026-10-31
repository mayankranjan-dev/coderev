import asyncio
import json

import httpx
import pytest

import main
from reviewer import providers
from reviewer.providers import OllamaProvider, ProviderError, StubProvider


def make(handler, **kwargs):
    defaults = dict(base_url="http://ollama.test", model="m", timeout_s=5, num_ctx=4096)
    defaults.update(kwargs)
    return OllamaProvider(transport=httpx.MockTransport(handler), **defaults)


def test_payload_sets_num_ctx_seed_and_temperature():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"response": "ok", "prompt_eval_count": 10, "eval_count": 3})

    result = asyncio.run(make(handler, num_ctx=16384, temperature=0.1).generate("p", seed=7))
    assert seen["options"] == {"temperature": 0.1, "num_ctx": 16384, "seed": 7}
    assert seen["model"] == "m" and seen["stream"] is False and seen["prompt"] == "p"
    assert result.text == "ok"
    assert result.prompt_eval_count == 10 and result.eval_count == 3
    assert result.num_ctx == 16384
    assert not result.truncation_suspected


def test_seed_omitted_when_not_given():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"response": ""})

    asyncio.run(make(handler).generate("p"))
    assert "seed" not in seen["options"]


def test_truncation_suspected_when_prompt_fills_context():
    def handler(request):
        return httpx.Response(200, json={"response": "x", "prompt_eval_count": 4096})
    assert asyncio.run(make(handler).generate("p")).truncation_suspected


def test_num_ctx_must_be_positive():
    with pytest.raises(ValueError):
        OllamaProvider("http://x", "m", 1, num_ctx=0)


@pytest.mark.parametrize(
    "raise_or_respond,kind",
    [
        (httpx.ConnectError("refused"), providers.UNREACHABLE),
        (httpx.ConnectTimeout("t"), providers.UNREACHABLE),
        (httpx.ReadTimeout("t"), providers.TIMEOUT),
        (httpx.Response(500), providers.HTTP_ERROR),
        (httpx.Response(200, text="not json"), providers.BAD_RESPONSE),
        (httpx.Response(200, json=["list"]), providers.BAD_RESPONSE),
        (httpx.Response(200, json={"response": 5}), providers.BAD_RESPONSE),
        (httpx.RemoteProtocolError("dropped"), providers.BAD_RESPONSE),
    ],
)
def test_failures_raise_typed_provider_error(raise_or_respond, kind):
    def handler(request):
        if isinstance(raise_or_respond, Exception):
            raise raise_or_respond
        return raise_or_respond

    with pytest.raises(ProviderError) as info:
        asyncio.run(make(handler).generate("p"))
    assert info.value.kind == kind


def test_is_available():
    assert asyncio.run(make(lambda r: httpx.Response(200, json={})).is_available())
    assert not asyncio.run(make(lambda r: httpx.Response(404)).is_available())

    def down(request):
        raise httpx.ConnectError("x")

    assert not asyncio.run(make(down).is_available())


def test_stub_provider_scripts_replies_and_records_calls():
    stub = StubProvider(replies=["a", ProviderError(providers.TIMEOUT, "t"), lambda p, s: f"{p}:{s}"])
    assert asyncio.run(stub.generate("x", seed=1)).text == "a"
    with pytest.raises(ProviderError):
        asyncio.run(stub.generate("y"))
    assert asyncio.run(stub.generate("z", seed=3)).text == "z:3"
    assert asyncio.run(stub.generate("w", seed=4)).text == "w:4"  # last reply repeats
    assert [c[0] for c in stub.calls] == ["x", "y", "z", "w"]


def test_production_provider_uses_configured_num_ctx(monkeypatch):
    monkeypatch.setattr(main, "OLLAMA_NUM_CTX", 12345)
    monkeypatch.setattr(main, "OLLAMA_MODEL", "some-model")
    provider = main.get_review_provider()
    assert provider.num_ctx == 12345 and provider.model == "some-model"
    assert provider.build_payload("p", None)["options"]["num_ctx"] == 12345
