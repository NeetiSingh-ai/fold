"""Thin wrapper over the Google Gen AI SDK.

Uses a Gemini API key if GEMINI_API_KEY is set, otherwise Vertex AI with the
Cloud Run service account. Every call returns parsed JSON plus real token usage
and the dollar cost computed from it.
"""
import json
import logging
import os
import random
import re
import time

log = logging.getLogger("fold")

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")            # the production workload model
COMPILER_MODEL = os.getenv("COMPILER_MODEL", MODEL)              # the model that writes the code
PRICE_IN = float(os.getenv("PRICE_IN_PER_M", "0.75"))            # USD per 1M input tokens
PRICE_OUT = float(os.getenv("PRICE_OUT_PER_M", "3.75"))          # USD per 1M output tokens (incl. thinking)
MOCK = os.getenv("MOCK_GEMINI") == "1"                           # local tests only

_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        http = types.HttpOptions(timeout=int(os.getenv("GEMINI_TIMEOUT_MS", "180000")))
        if os.getenv("GEMINI_API_KEY"):
            _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=http)
        else:
            _client = genai.Client(vertexai=True, project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                                   location=os.getenv("VERTEX_LOCATION", "global"), http_options=http)
    return _client


def parse_json(text: str):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            return json.loads(text[s:e + 1])
        raise


def generate_json(prompt: str, model: str | None = None, temperature: float = 0.0):
    if MOCK:
        from mock_gemini import mock_generate
        return mock_generate(prompt)
    from google.genai import types
    t0 = time.time()
    cfg = types.GenerateContentConfig(temperature=temperature, response_mime_type="application/json")
    last = None
    for attempt in range(4):  # retry rate limits / transient errors with backoff
        try:
            resp = _get_client().models.generate_content(model=model or MODEL, contents=prompt, config=cfg)
            out = parse_json(resp.text)
            break
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("gemini call failed (attempt %d): %s", attempt + 1, e)
            time.sleep(2 * (attempt + 1) + random.random())
    else:
        raise last
    u = resp.usage_metadata
    tin = (u.prompt_token_count or 0) if u else 0
    tout = ((u.candidates_token_count or 0) + (getattr(u, "thoughts_token_count", 0) or 0)) if u else 0
    usage = {"tokens_in": tin, "tokens_out": tout,
             "cost_usd": tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT,
             "latency_ms": int((time.time() - t0) * 1000), "model": model or MODEL}
    return out, usage
