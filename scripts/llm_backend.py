#!/usr/bin/env python3
"""Multi-provider LLM backend — route each pipeline stage to a provider/model.

Providers:
  anthropic   — direct Anthropic API (anthropic SDK). The default everywhere.
  openrouter  — OpenRouter (USDC-fundable). anthropic/* models go through OR's
                native Anthropic-format endpoint via the anthropic SDK
                (base_url + Bearer auth); every other model uses the
                OpenAI-format chat/completions endpoint (urllib, no new deps).
  aster       — Aster Labs (api.asterlab.ai/v1), OpenAI-format.

Per-stage routing lives in config.json (all optional — no "backends" key means
direct Anthropic everywhere, byte-identical to previous behavior):

  "backends": {
    "default":      {"provider": "anthropic"},
    "opus_pick":    {"provider": "openrouter", "model": "anthropic/claude-opus-5"},
    "idea_matcher": {"provider": "aster", "model": "glm-5.2", "json_mode": true},
    "call_extract": {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.5",
                     "or_provider": {"order": ["amazon-bedrock", "google-vertex"],
                                     "allow_fallbacks": false, "zdr": true}}
  }

Env keys: ANTHROPIC_API_KEY, OPENROUTER_API_KEY, ASTER_API_KEY.

chat(stage, system_text, user_text, max_tokens, default_model)
  -> (text, usage_dict, cost_usd)
Raises RuntimeError on output truncation (parity with the old per-site checks).
Reasoning-model <think> blocks are stripped defensively from OpenAI-format
responses so JSON parsing downstream stays clean.
"""
import os, json, re, urllib.request, urllib.error

OPENROUTER_ANTHROPIC_BASE = "https://openrouter.ai/api"
OPENROUTER_OPENAI_BASE = "https://openrouter.ai/api/v1"
ASTER_BASE = "https://api.asterlab.ai/v1"

# $/M tokens: (input, output, cache_write, cache_read). Cost REPORTING only —
# unknown models bill fine, they just report $0 with a note in usage.
_PRICES = [
    # (provider-or-None, model-substring, in, out, cache_w, cache_r)
    (None,        "opus-5",        5.00, 25.00, 6.25, 0.50),
    (None,        "opus-4",        5.00, 25.00, 6.25, 0.50),
    (None,        "sonnet-5",      2.00, 10.00, 2.50, 0.20),   # intro pricing thru 2026-08-31
    (None,        "sonnet-4",      3.00, 15.00, 3.75, 0.30),
    (None,        "haiku",         1.00,  5.00, 1.25, 0.10),
    ("aster",     "kimi-k3",       2.80, 15.00, 0.00, 0.28),
    (None,        "kimi-k3",       3.00, 15.00, 0.00, 0.30),
    ("aster",     "glm-5.2",       1.00,  4.00, 0.00, 0.20),
    (None,        "glm-5.2",       0.49,  1.54, 0.00, 0.10),   # OR Z.AI promo (2026-08-25)
    ("aster",     "gpt-oss-120b",  0.15,  0.60, 0.00, 0.00),
    (None,        "gpt-oss-120b",  0.03,  0.17, 0.00, 0.00),
    (None,        "deepseek",      0.14,  0.28, 0.00, 0.028),
]

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def _load_backends():
    """Read the 'backends' map through the same canonical config chain the rest
    of the pipeline uses (import lazily to avoid cycles)."""
    try:
        from gather_x import load_config
        cfg = load_config() or {}
        return cfg.get("backends") or {}
    except Exception:
        return {}


_BACKENDS = None


def _backend_for(stage):
    global _BACKENDS
    if _BACKENDS is None:
        _BACKENDS = _load_backends()
    b = _BACKENDS.get(stage) or _BACKENDS.get("default") or {}
    return dict(b)


def _price_for(provider, model):
    m = (model or "").lower()
    for prov, sub, i, o, cw, cr in _PRICES:
        if sub in m and (prov is None or prov == provider):
            return i, o, cw, cr
    return None


def _cost(provider, model, usage):
    p = _price_for(provider, model)
    if not p:
        usage["_pricing"] = "unknown-model: cost reported as 0"
        return 0.0
    i, o, cw, cr = p
    return (usage.get("input_tokens", 0) * i
            + usage.get("output_tokens", 0) * o
            + usage.get("cache_creation_input_tokens", 0) * cw
            + usage.get("cache_read_input_tokens", 0) * cr) / 1_000_000


def _anthropic_chat(model, system_text, user_text, max_tokens,
                    base_url=None, auth_token=None, or_provider=None):
    import anthropic
    kwargs = {}
    if base_url:
        # OpenRouter's native Anthropic-format endpoint: Bearer auth, not x-api-key
        kwargs["base_url"] = base_url
        kwargs["auth_token"] = auth_token
        kwargs["api_key"] = None
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        kwargs["api_key"] = api_key
    client = anthropic.Anthropic(**kwargs)
    create_kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system_text,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_text}],
    )
    if or_provider:
        create_kwargs["extra_body"] = {"provider": or_provider}
    resp = client.messages.create(**create_kwargs)
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise RuntimeError(f"{model}: output truncated at max_tokens")
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "").strip()
    u = resp.usage
    usage = {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
    }
    return text, usage


def _openai_chat(base, api_key, model, system_text, user_text, max_tokens,
                 json_mode=False, or_provider=None, timeout=600, stream=False,
                 nothink=False):
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if or_provider:
        payload["provider"] = or_provider
    if nothink:
        if base == OPENROUTER_OPENAI_BASE:
            # OpenRouter's unified reasoning control (vLLM kwargs don't pass through)
            payload["reasoning"] = {"enabled": False}
        else:
            # vLLM-style thinking toggle (verified on Aster GLM: 8x fewer output
            # tokens). Reasoning burn otherwise exceeds Aster's ~300s gateway
            # wall on large calls.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
    if stream:
        # Streaming keeps bytes flowing from the first token, so serverless
        # gateways (Aster 504s buffered responses at ~5 min) don't kill slow
        # models mid-generation. urllib's timeout is per-read, not total.
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if stream:
                parts, finish, u = [], None, {}
                for raw_line in r:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        # Streamed errors arrive as HTTP-200 data chunks
                        err = str(chunk["error"])[:200]
                        if json_mode and ("grammar" in err or "response_format" in err):
                            return _openai_chat(base, api_key, model, system_text,
                                                user_text, max_tokens, json_mode=False,
                                                or_provider=or_provider,
                                                timeout=timeout, stream=stream,
                                                nothink=nothink)
                        raise RuntimeError(f"{model}: {err}")
                    if chunk.get("usage"):
                        u = chunk["usage"]
                    for ch in chunk.get("choices") or []:
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
                        delta = (ch.get("delta") or {}).get("content")
                        if delta:
                            parts.append(delta)
                out = {"choices": [{"finish_reason": finish,
                                    "message": {"content": "".join(parts)}}],
                       "usage": u}
            else:
                out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        # Some serving stacks (e.g. Aster's gpt-oss speculative decoding) reject
        # response_format entirely — retry once without it, prompt still asks for JSON
        if json_mode and e.code == 400 and ("grammar" in detail or "response_format" in detail):
            return _openai_chat(base, api_key, model, system_text, user_text,
                                max_tokens, json_mode=False,
                                or_provider=or_provider, timeout=timeout,
                                stream=stream, nothink=nothink)
        raise RuntimeError(f"{model}: HTTP {e.code} {detail}")
    if out.get("error"):
        raise RuntimeError(f"{model}: {str(out['error'])[:200]}")
    choice = (out.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError(f"{model}: output truncated (finish_reason=length)")
    text = ((choice.get("message") or {}).get("content") or "").strip()
    # Reasoning models sometimes leak thinking blocks into content
    text = _THINK_RE.sub("", text).strip()
    u = out.get("usage") or {}
    if not u:
        # Stream ended without a usage chunk — rough chars/4 estimate so cost
        # logs stay populated rather than silently reading $0
        u = {"prompt_tokens": (len(system_text) + len(user_text)) // 4,
             "completion_tokens": len(text) // 4}
    cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")
              or u.get("cached_tokens") or 0)
    usage = {
        "input_tokens": max((u.get("prompt_tokens") or 0) - cached, 0),
        "output_tokens": u.get("completion_tokens") or 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }
    return text, usage


def chat(stage, system_text, user_text, max_tokens, default_model):
    """Route one completion for `stage`. Returns (text, usage, cost_usd)."""
    b = _backend_for(stage)
    provider = (b.get("provider") or "anthropic").lower()
    model = b.get("model") or default_model
    or_provider = b.get("or_provider")
    json_mode = bool(b.get("json_mode"))

    if provider == "anthropic":
        text, usage = _anthropic_chat(model, system_text, user_text, max_tokens)
    elif provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        if model.startswith("anthropic/") or model.startswith("claude"):
            text, usage = _anthropic_chat(
                model, system_text, user_text, max_tokens,
                base_url=OPENROUTER_ANTHROPIC_BASE, auth_token=key,
                or_provider=or_provider)
        else:
            text, usage = _openai_chat(
                OPENROUTER_OPENAI_BASE, key, model, system_text, user_text,
                max_tokens, json_mode=json_mode, or_provider=or_provider,
                nothink=bool(b.get("nothink")))
    elif provider == "aster":
        key = os.environ.get("ASTER_API_KEY")
        if not key:
            raise RuntimeError("ASTER_API_KEY not set")
        text, usage = _openai_chat(
            ASTER_BASE, key, model, system_text, user_text, max_tokens,
            json_mode=json_mode, stream=True, nothink=bool(b.get("nothink")))
    else:
        raise RuntimeError(f"unknown provider '{provider}' for stage '{stage}'")

    usage["_provider"] = provider
    usage["_model"] = model
    return text, usage, _cost(provider, model, usage)
