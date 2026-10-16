"""Cloudflare Workers AI is reached through its OpenAI-compatible endpoint with a Bearer API token."""
import json

import httpx

from tessera.agents import PROVIDER_KINDS, Provider


def test_workers_ai_uses_openai_compatible_chat_completions():
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers.get("authorization"), json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    base = PROVIDER_KINDS["cloudflare_workers_ai"]["base_url"].replace("YOUR-ACCOUNT-ID", "abc123")
    p = Provider("cloudflare_workers_ai", base, "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "cf-token",
                 http_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    p.complete("system", [{"role": "user", "content": "hi"}], [])
    assert seen["url"] == "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1/chat/completions"
    assert seen["auth"] == "Bearer cf-token" and seen["body"]["model"].startswith("@cf/")
