import config, llm

class FakeChat:
    def __init__(self): self.kwargs = None
    _citations = None
    def create(self, **kw):
        self.kwargs = kw
        # emulate a streaming response: two content chunks, citations on the last
        def chunk(content, citations=None):
            delta = type("D", (), {"content": content})
            choice = type("Ch", (), {"delta": delta})
            return type("K", (), {"choices": [choice], "citations": citations})
        return iter([chunk("OPENAI-"), chunk("REPORT", self._citations)])

class FakeOpenAI:
    def __init__(self): self.chat = type("C", (), {"completions": FakeChat()})()

class _FakeStream:
    """Context manager mimicking the Anthropic SDK's streaming response:
    `with client.messages.stream(...) as s: for chunk in s.text_stream`."""
    def __init__(self, chunks): self._chunks = list(chunks)
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    @property
    def text_stream(self): return iter(self._chunks)

class FakeAnthropic:
    def __init__(self, chunks=("ANTHROPIC-", "REPORT")):
        self.kwargs = None; self.messages = self; self._chunks = chunks
    def stream(self, **kw):
        self.kwargs = kw
        return _FakeStream(self._chunks)

class FakeOverloadError(Exception):
    status_code = 503


class FakeGemini:
    def __init__(self, fail_first=0):
        self.calls = []; self.fail_first = fail_first
        self.models = self
    def generate_content(self, **kw):
        self.calls.append(kw["model"])
        if len(self.calls) <= self.fail_first:
            raise FakeOverloadError("503 overloaded")
        return type("R", (), {"text": "GEMINI-REPORT"})

def test_complete_openai_passes_max_tokens_and_system():
    prov = config.Provider("ds", "openai", "k", "deepseek-v4-pro", base_url="https://x", max_tokens=4096)
    client = FakeOpenAI()
    text = llm._complete_openai(client, prov, "SYS", "PROMPT")
    assert text == "OPENAI-REPORT"
    kw = client.chat.completions.kwargs
    assert kw["model"] == "deepseek-v4-pro" and kw["max_tokens"] == 4096
    assert kw["messages"][0] == {"role": "system", "content": "SYS"}
    assert kw["messages"][1]["content"] == "PROMPT"

def test_complete_openai_appends_citations_when_present():
    prov = config.Provider("exa", "openai", "k", "exa", max_tokens=4096)
    client = FakeOpenAI()
    client.chat.completions._citations = ["https://a.example/1", "https://b.example/2"]
    text = llm._complete_openai(client, prov, "SYS", "PROMPT")
    assert "OPENAI-REPORT" in text
    assert "https://a.example/1" in text and "Sources:" in text


def test_make_client_anthropic_honors_base_url():
    prov = config.Provider("glm", "anthropic", "k", "glm-5.2",
                           base_url="https://api.z.ai/api/anthropic")
    client = llm.make_client(prov)
    assert str(client.base_url).startswith("https://api.z.ai/api/anthropic")


def test_make_client_anthropic_default_base_url_when_unset():
    prov = config.Provider("claude", "anthropic", "k", "claude-opus-4-20250514")
    client = llm.make_client(prov)
    assert "api.anthropic.com" in str(client.base_url)


def test_complete_anthropic_uses_max_tokens_no_base_url():
    prov = config.Provider("claude", "anthropic", "k", "claude-opus-4-20250514", max_tokens=120000)
    client = FakeAnthropic()
    text = llm._complete_anthropic(client, prov, "SYS", "PROMPT")
    assert text == "ANTHROPIC-REPORT" and client.kwargs["max_tokens"] == 120000
    assert client.kwargs["model"] == "claude-opus-4-20250514"
    assert client.kwargs["system"] == "SYS"
    assert all(m["role"] != "system" for m in client.kwargs["messages"])

def test_complete_gemini_uses_max_output_tokens_and_walks_fallback():
    prov = config.Provider("g", "gemini", "k", "gemini-2.5-pro", max_tokens=65536,
                           fallback_models=("gemini-2.5-flash",))
    client = FakeGemini(fail_first=1)
    text = llm._complete_gemini(client, prov, "SYS", "PROMPT")
    assert text == "GEMINI-REPORT"
    assert client.calls == ["gemini-2.5-pro", "gemini-2.5-flash"]


# overload-shaped errors walk the chain; non-overload errors propagate immediately

class FakeGeminiAlwaysOverloaded:
    """All models raise an overload-shaped error (status_code=503)."""
    def __init__(self):
        self.calls = []
        self.models = self

    def generate_content(self, **kw):
        self.calls.append(kw["model"])
        raise FakeOverloadError("overloaded")


def test_complete_gemini_all_overloaded_raises_runtime_error():
    """When every model raises an overload error, _complete_gemini raises RuntimeError mentioning 'All Gemini models failed'."""
    prov = config.Provider("g", "gemini", "k", "gemini-2.5-pro", max_tokens=65536,
                           fallback_models=("gemini-2.5-flash",))
    client = FakeGeminiAlwaysOverloaded()
    try:
        llm._complete_gemini(client, prov, "SYS", "PROMPT")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "All Gemini models failed" in str(e)
    # Both models were attempted (walked the fallback chain)
    assert client.calls == ["gemini-2.5-pro", "gemini-2.5-flash"]


class FakeGeminiNonOverload:
    """Raises a plain ValueError (non-overload) on every call."""
    def __init__(self):
        self.calls = []
        self.models = self

    def generate_content(self, **kw):
        self.calls.append(kw["model"])
        raise ValueError("bad key")


def test_complete_gemini_non_overload_propagates_immediately():
    """A non-overload error must propagate immediately without trying fallback models."""
    prov = config.Provider("g", "gemini", "k", "gemini-2.5-pro", max_tokens=65536,
                           fallback_models=("gemini-2.5-flash",))
    client = FakeGeminiNonOverload()
    try:
        llm._complete_gemini(client, prov, "SYS", "PROMPT")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "bad key" in str(e)
    # Only the first model was tried — fallback NOT attempted
    assert client.calls == ["gemini-2.5-pro"]


def test_complete_openai_empty_response_raises():
    class _EmptyChat:
        def create(self, **kw):
            delta = type("D", (), {"content": None})
            choice = type("Ch", (), {"delta": delta})
            return iter([type("K", (), {"choices": [choice], "citations": None})])
    client = type("X", (), {"chat": type("Y", (), {"completions": _EmptyChat()})()})()
    prov = config.Provider("p", "openai", "k", "m")
    import pytest
    with pytest.raises(RuntimeError, match="empty response"):
        llm._complete_openai(client, prov, "SYS", "PROMPT")


def test_complete_anthropic_empty_content_list_raises():
    class _C:
        def __init__(self): self.messages = self
        def stream(self, **kw):
            # An empty text_stream -> joined text "" -> the empty-response path.
            return _FakeStream([])
    client = _C()
    prov = config.Provider("p", "anthropic", "k", "m")
    import pytest
    with pytest.raises(RuntimeError, match="empty response"):
        llm._complete_anthropic(client, prov, "SYS", "PROMPT")

def test_complete_openai_empty_choices_raises():
    class _Chat:
        def create(self, **kw): return iter([type("K", (), {"choices": [], "citations": None})])
    client = type("X", (), {"chat": type("Y", (), {"completions": _Chat()})()})()
    prov = config.Provider("p", "openai", "k", "m")
    import pytest
    with pytest.raises(RuntimeError, match="empty response"):
        llm._complete_openai(client, prov, "SYS", "PROMPT")


def test_complete_cli_generic_pipes_system_and_prompt():
    # 'cat' echoes stdin; generic path sends "SYS\n\nPROMPT"
    prov = config.Provider("sub", "cli", "", "", command="cat")
    text = llm._complete_cli(None, prov, "SYS", "PROMPT")
    assert text == "SYS\n\nPROMPT"

def test_complete_cli_scrubs_api_keys_from_subprocess_env(tmp_path, monkeypatch):
    # a script that reports both ANTHROPIC_API_KEY and OPENAI_API_KEY values
    script = tmp_path / "probe.sh"
    script.write_text('#!/bin/sh\necho "A=$ANTHROPIC_API_KEY|O=$OPENAI_API_KEY"\n')
    script.chmod(0o755)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-should-be-scrubbed")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-be-scrubbed")
    prov = config.Provider("sub", "cli", "", "", command=str(script))
    text = llm._complete_cli(None, prov, "SYS", "PROMPT")
    assert "sk-anthropic-should-be-scrubbed" not in text
    assert "sk-openai-should-be-scrubbed" not in text
    assert "A=|O=" in text

def test_complete_cli_nonzero_exit_raises(tmp_path):
    script = tmp_path / "fail.sh"
    script.write_text('#!/bin/sh\necho "boom" >&2\nexit 3\n'); script.chmod(0o755)
    prov = config.Provider("sub", "cli", "", "", command=str(script))
    import pytest
    with pytest.raises(RuntimeError, match="exited 3"):
        llm._complete_cli(None, prov, "SYS", "PROMPT")

def test_cli_argv_builder_claude_and_codex():
    pc = config.Provider("c", "cli", "", "claude-opus-4-20250514", command="/usr/local/bin/claude")
    argv, stdin = llm._cli_argv_and_input(pc, "SYSPROMPT", "USERPROMPT")
    assert argv[:4] == ["/usr/local/bin/claude", "-p", "--system-prompt", "SYSPROMPT"]
    assert "--model" in argv and "claude-opus-4-20250514" in argv and stdin == "USERPROMPT"
    px = config.Provider("x", "cli", "", "", command="codex")
    argv2, stdin2 = llm._cli_argv_and_input(px, "SYSPROMPT", "USERPROMPT")
    assert argv2[:2] == ["codex", "exec"]
    assert "--ephemeral" in argv2
    assert argv2[argv2.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv2
    assert argv2[-1] == "-"
    assert "--model" not in argv2 and stdin2 == "SYSPROMPT\n\nUSERPROMPT"


def test_cli_extra_args_appended():
    p = config.Provider("c", "cli", "", "", command="claude", extra_args=("--allowedTools", "WebSearch"))
    argv, _ = llm._cli_argv_and_input(p, "SYS", "PROMPT")
    assert argv[-2:] == ["--allowedTools", "WebSearch"]


def test_complete_cli_scrubs_api_keys_from_subprocess_env_both_keys(tmp_path, monkeypatch):
    # Probe both ANTHROPIC_API_KEY and OPENAI_API_KEY
    script = tmp_path / "probe2.sh"
    script.write_text('#!/bin/sh\necho "A=$ANTHROPIC_API_KEY|O=$OPENAI_API_KEY"\n')
    script.chmod(0o755)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    prov = config.Provider("sub", "cli", "", "", command=str(script))
    text = llm._complete_cli(None, prov, "SYS", "PROMPT")
    assert "sk-anthropic-secret" not in text
    assert "sk-openai-secret" not in text
    assert "A=|O=" in text


def test_call_model_routes_by_api_type():
    # 'cat' echoes stdin; generic CLI branch sends "SYS\n\nUSER"
    prov = config.Provider("sub", "cli", "", "", command="cat")
    result = llm.call_model(prov, "SYS", "USER")
    assert result == "SYS\n\nUSER"
