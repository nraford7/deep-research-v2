"""llm.py — model-call runtime: turn a Provider + prompts into text.

The single primitive (`call_model`) used by dispatch.py (Round 1) and scope.py
(Round 0), so every model call routes through the config.py provider registry.
No TOML parsing here (that is config.py); no orchestration (that is dispatch.py).
"""

import os
import subprocess

CLI_TIMEOUT_S = 1800  # CLI reports are long; generous timeout


def make_client(provider):
    if provider.api_type == "cli":
        return None                                # no SDK client; subprocess handles auth
    if provider.api_type == "anthropic":
        import anthropic
        return anthropic.Anthropic(api_key=provider.api_key)
    if provider.api_type == "gemini":
        from google import genai
        return genai.Client(api_key=provider.api_key)
    from openai import OpenAI                      # openai-compatible (perplexity/grok/deepseek/glm/openrouter/fireworks)
    kwargs = {"api_key": provider.api_key}
    if provider.base_url:
        kwargs["base_url"] = provider.base_url
    return OpenAI(**kwargs)


def _complete_openai(client, provider, system_prompt, user_prompt):
    # GPT-5+ rejects `max_tokens`, requiring `max_completion_tokens`. Other
    # OpenAI-compatible endpoints (perplexity, grok, gpt-4.x) still use max_tokens.
    m = (provider.model or "").lower()
    token_kw = "max_completion_tokens" if m.startswith(("gpt-5", "o1", "o3", "o4")) else "max_tokens"
    resp = client.chat.completions.create(
        model=provider.model,
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_prompt}],
        **{token_kw: provider.max_tokens},
    )
    if not resp.choices or not resp.choices[0].message.content:
        raise RuntimeError(f"provider '{provider.name}' returned an empty response")
    return resp.choices[0].message.content


def _complete_anthropic(client, provider, system_prompt, user_prompt):
    # Stream: the SDK refuses non-streaming requests that may exceed 10 min,
    # which large max_tokens (e.g. 128k) reliably trips for long reports.
    parts = []
    with client.messages.stream(
        model=provider.model, max_tokens=provider.max_tokens,
        system=system_prompt, messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        for chunk in stream.text_stream:
            parts.append(chunk)
    text = "".join(parts)
    if not text.strip():
        raise RuntimeError(f"provider '{provider.name}' returned an empty response")
    return text


def _is_gemini_overload(exc):
    for attr in ("status_code", "code"):
        c = getattr(exc, attr, None)
        if isinstance(c, int) and c in (429, 500, 503):
            return True
    return type(exc).__name__ in ("ServerError", "ResourceExhausted", "UnavailableError")


def _complete_gemini(client, provider, system_prompt, user_prompt):
    from google.genai import types as genai_types
    full = f"{system_prompt}\n\n{user_prompt}"
    last = None
    for model_id in [provider.model, *provider.fallback_models]:
        try:
            resp = client.models.generate_content(
                model=model_id, contents=full,
                config=genai_types.GenerateContentConfig(max_output_tokens=provider.max_tokens),
            )
            text = resp.text
            if not text:
                raise RuntimeError(f"provider '{provider.name}' returned an empty response")
            return text
        except Exception as e:
            if not _is_gemini_overload(e):
                raise
            last = e
    raise RuntimeError(f"All Gemini models failed for provider '{provider.name}': {last}")


def _cli_argv_and_input(provider, system_prompt, user_prompt):
    base = os.path.basename(provider.command)
    if base == "claude":
        argv = [provider.command, "-p", "--system-prompt", system_prompt]
        if provider.model:
            argv += ["--model", provider.model]
        argv += list(provider.extra_args)
        return argv, user_prompt                   # user prompt via stdin
    if base == "codex":
        argv = [provider.command, "exec"]
        if provider.model:
            argv += ["--model", provider.model]
        argv += list(provider.extra_args)
        return argv, f"{system_prompt}\n\n{user_prompt}"  # system prepended (no dedicated flag)
    argv = [provider.command] + list(provider.extra_args)
    return argv, f"{system_prompt}\n\n{user_prompt}"


def _complete_cli(client, provider, system_prompt, user_prompt):
    argv, stdin_text = _cli_argv_and_input(provider, system_prompt, user_prompt)
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # force subscription auth, not metered API
    env.pop("OPENAI_API_KEY", None)
    proc = subprocess.run(argv, input=stdin_text, capture_output=True, text=True,
                          env=env, timeout=CLI_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(f"provider '{provider.name}' CLI exited {proc.returncode}: "
                           f"{proc.stderr.strip()[:500]}")
    text = proc.stdout.strip()
    if not text:
        raise RuntimeError(f"provider '{provider.name}' returned an empty response")
    return text


COMPLETERS = {"openai": _complete_openai, "anthropic": _complete_anthropic,
              "gemini": _complete_gemini, "cli": _complete_cli}


def call_model(provider, system_prompt, user_prompt):
    """Return the model's text for a Provider + prompts. Routes on provider.api_type."""
    client = make_client(provider)
    return COMPLETERS[provider.api_type](client, provider, system_prompt, user_prompt)
