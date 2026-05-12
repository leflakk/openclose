"""LLM provider abstraction.

Today only the OpenAI-compatible ``Provider`` is registered, but the
``make_provider`` factory dispatches on ``ProviderConfig.kind`` so new
provider implementations can be added by registering one factory branch —
no schema change, no call-site refactor.

All providers MUST yield OpenAI ``ChatCompletionChunk`` from ``chat()``
regardless of their native wire format. The agent loop consumes
``chunk.choices[0].delta.{content,tool_calls}`` directly, so any future
Anthropic/Google provider has to translate its events into OpenAI shape
internally. This keeps the streaming contract uniform without a
normalization layer in the middle.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Protocol, runtime_checkable

import httpx
from openai import AsyncOpenAI, BadRequestError
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionChunk

from openclose.config.config import get_config
from openclose.config.schema import ProviderConfig
from openclose.provider.auth import load_api_key
from openclose.log import get_logger

log = get_logger(__name__)

# Per-chunk idle-gap timeout for streaming completions. If the server
# returns 200 headers but then stalls (no SSE chunks arrive for this long),
# abort instead of waiting forever. p99 legit chunk gap is <60s even for
# verbose tool-call generations.
_STREAM_IDLE_TIMEOUT_S = 90.0


@runtime_checkable
class BaseProvider(Protocol):
    """Streaming-LLM contract every provider class must satisfy.

    ``chat`` MUST yield OpenAI ``ChatCompletionChunk`` regardless of the
    upstream wire format — the agent loop consumes that shape directly.
    """

    # ``chat`` is declared without ``async`` so async-generator implementations
    # (``async def`` + ``yield``) satisfy the Protocol — Python types an
    # async generator as a plain function returning ``AsyncIterator``, not
    # as a coroutine returning one.
    def chat(
        self,
        messages: list[ChatCompletionMessageParam],
        model: str,
        tools: list[dict[str, Any]] | None = ...,
        temperature: float = ...,
        max_tokens: int | None = ...,
        tool_choice: str | None = ...,
    ) -> AsyncIterator[ChatCompletionChunk]: ...

    async def chat_sync(
        self,
        messages: list[ChatCompletionMessageParam],
        model: str,
        tools: list[dict[str, Any]] | None = ...,
        temperature: float = ...,
        max_tokens: int | None = ...,
        tool_choice: str | None = ...,
    ) -> Any: ...

    async def detect_model(self) -> str | None: ...

    @property
    def client(self) -> AsyncOpenAI: ...


class Provider:
    """OpenAI-compatible LLM provider.

    Works with OpenAI, vLLM, llama.cpp, Ollama, and any endpoint
    implementing the OpenAI chat completions API.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "",
        provider_name: str = "default",
    ) -> None:
        resolved_key = api_key or load_api_key(provider_name)
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=resolved_key or "no-key",
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
            max_retries=2,
        )
        self._provider_name = provider_name
        log.debug("Provider initialized: %s @ %s", provider_name, base_url)

    @property
    def client(self) -> AsyncOpenAI:
        return self._client

    async def detect_model(self) -> str | None:
        """Auto-detect the first available model from the endpoint."""
        try:
            models = await self._client.models.list()
            for m in models.data:
                log.info("Auto-detected model: %s", m.id)
                return m.id
        except Exception as e:
            log.warning("Failed to auto-detect model: %s", e)
        return None

    @staticmethod
    def _wrap_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Wrap flat tool schemas into OpenAI function-calling format."""
        return [{"type": "function", "function": t} for t in tools]

    def _maybe_dump(
        self,
        messages: list[Any],
        model: str,
        temperature: float,
        tools: list[dict[str, Any]] | None,
    ) -> None:
        """Log this LLM call via dump_llm_request() if a debug context is set.

        The contextvar is set by the caller (agent loop, compaction path,
        or a sub-agent tool like browser_automation) so that every LLM request
        — regardless of entry point — flows through the same debug dump.
        No-op when the contextvar is unset; dump_llm_request() is itself
        a no-op when OPENCLOSE_DEBUG_LLM is off.
        """
        from openclose.debug import dump_llm_request, llm_debug_context

        ctx = llm_debug_context.get(None)
        if ctx is None:
            return
        dump_llm_request(
            step=ctx.step,
            source=ctx.source,
            model=model,
            temperature=temperature,
            messages=messages,
            tools=tools,
            project_dir=ctx.project_dir,
        )

    @staticmethod
    def _wrap_tool_calls_in_messages(
        messages: list[Any],
    ) -> list[Any]:
        """Wrap flat tool_calls in assistant messages to OpenAI format.

        Internally, tool_calls are stored flat (``{id, name, arguments}``)
        to save context tokens.  The OpenAI API expects the nested format
        (``{id, type, function: {name, arguments}}``), so we wrap here —
        at the API boundary — just like ``_wrap_tools`` does for schemas.
        """
        out: list[Any] = []
        for msg in messages:
            if not isinstance(msg, dict):
                out.append(msg)
                continue
            tcs = msg.get("tool_calls")
            if not isinstance(tcs, list):
                out.append(msg)
                continue
            wrapped_tcs = []
            for tc in tcs:
                if isinstance(tc, dict) and "type" not in tc:
                    args = tc.get("arguments", "")
                    # Defensive: the API rejects the whole turn if any
                    # tool_call's arguments isn't parseable JSON. Loop-side
                    # recovery handles the common parallel-streaming case;
                    # this is the last-ditch safety net for anything that
                    # slipped through (e.g. truncated single tool call).
                    if isinstance(args, str) and args:
                        try:
                            json.loads(args)
                        except json.JSONDecodeError:
                            log.warning(
                                "tool_call arguments not valid JSON; "
                                "normalizing to '{}' to avoid API 400 "
                                "(raw=%r)", args[:200],
                            )
                            args = "{}"
                    wrapped_tcs.append({
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": args,
                        },
                    })
                else:
                    wrapped_tcs.append(tc)
            out.append({**msg, "tool_calls": wrapped_tcs})
        return out

    async def chat(
        self,
        messages: list[ChatCompletionMessageParam],
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Send a chat completion request and stream the response."""
        self._maybe_dump(list(messages), model, temperature, tools)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._wrap_tool_calls_in_messages(messages),
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = self._wrap_tools(tools)
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except BadRequestError as e:
            # Some OpenAI-compatible endpoints (vLLM, llama.cpp, Ollama)
            # reject tool_choice="required". Retry once without it so the
            # caller's behavior degrades to default "auto" instead of failing.
            if "tool_choice" in str(e) and "tool_choice" in kwargs:
                log.warning(
                    "Endpoint rejected tool_choice=%r; retrying without it",
                    tool_choice,
                )
                kwargs.pop("tool_choice", None)
                response = await self._client.chat.completions.create(**kwargs)
            else:
                raise
        aiter = response.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    aiter.__anext__(), timeout=_STREAM_IDLE_TIMEOUT_S,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                log.warning(
                    "LLM stream idle > %.0fs, aborting", _STREAM_IDLE_TIMEOUT_S,
                )
                await response.close()
                raise
            yield chunk

    async def chat_sync(
        self,
        messages: list[ChatCompletionMessageParam],
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        """Non-streaming chat completion."""
        self._maybe_dump(list(messages), model, temperature, tools)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._wrap_tool_calls_in_messages(messages),
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            kwargs["tools"] = self._wrap_tools(tools)
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        try:
            return await self._client.chat.completions.create(**kwargs)
        except BadRequestError as e:
            if "tool_choice" in str(e) and "tool_choice" in kwargs:
                log.warning(
                    "Endpoint rejected tool_choice=%r; retrying without it",
                    tool_choice,
                )
                kwargs.pop("tool_choice", None)
                return await self._client.chat.completions.create(**kwargs)
            raise


_providers: dict[str, BaseProvider] = {}


def make_provider(cfg: ProviderConfig) -> BaseProvider:
    """Instantiate a provider from its config.

    Dispatches on ``cfg.kind``. Adding a new provider class later means
    adding one branch here — no schema change, no caller change.
    """
    if cfg.kind == "openai_compatible":
        return Provider(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            provider_name=cfg.name,
        )
    raise NotImplementedError(
        f"Provider kind {cfg.kind!r} not implemented "
        f"(provider name={cfg.name!r})"
    )


def _resolve_provider_name(requested: str) -> str:
    """Pick the effective provider name: explicit > default_provider > first."""
    config = get_config()
    if requested:
        return requested
    if config.default_provider:
        return config.default_provider
    if config.providers:
        return config.providers[0].name
    return "default"


def get_provider(provider_name: str = "") -> BaseProvider:
    """Get or create a provider, cached by name.

    Empty ``provider_name`` resolves to the configured default. Unknown
    names fall back to the first declared provider (best-effort, matches
    the prior singleton's behavior for legacy callers).
    """
    name = _resolve_provider_name(provider_name)
    if name in _providers:
        return _providers[name]

    config = get_config()
    cfg = next((p for p in config.providers if p.name == name), None)
    if cfg is None:
        # Caller asked for a provider that isn't declared. Use the first
        # one if any, otherwise a built-in default — same fallback the
        # old singleton applied.
        cfg = config.providers[0] if config.providers else ProviderConfig()

    inst = make_provider(cfg)
    _providers[cfg.name] = inst
    # Also cache under the requested alias so future lookups for an
    # unknown name short-circuit to the same instance instead of
    # re-walking the config.
    _providers[name] = inst
    return inst


def reset_provider_cache() -> None:
    """Drop all cached provider instances. For tests and config reload."""
    _providers.clear()
