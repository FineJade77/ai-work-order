import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from functools import lru_cache

from openai import AsyncOpenAI

from app.core.config import get_settings
from app.genai_tracing import ChatSpan
from app.tenancy import parse_tenant_id
from app.usage import record_usage


class ModelProvider(ABC):
    @abstractmethod
    def generate(
        self, *, messages: list[dict], system_prompt: str, tenant_id: str
    ) -> AsyncIterator[dict]: ...


class OpenAICompatibleProvider(ModelProvider):
    """Talks to any provider exposing an OpenAI-compatible /chat/completions endpoint.

    Swapping providers (DeepSeek today, a future one tomorrow) is a base_url/api_key/model
    config change, not a code change (architecture-design.md section 3.5).
    """

    def __init__(
        self, *, base_url: str, api_key: str, model: str, provider_name: str = "openai-compatible",
        capture_content: bool = False,
    ):
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._base_url = base_url
        self._model = model
        self._provider_name = provider_name
        self._capture_content = capture_content

    async def generate(
        self, *, messages: list[dict], system_prompt: str, tenant_id: str
    ) -> AsyncIterator[dict]:
        parse_tenant_id(tenant_id)

        start = time.monotonic()
        prompt_tokens = 0
        completion_tokens = 0
        status = "ok"
        span = ChatSpan(
            provider=self._provider_name, model=self._model, base_url=self._base_url, tenant_id=tenant_id,
            capture_content=self._capture_content, system_prompt=system_prompt, messages=messages,
        )
        error: BaseException | None = None
        try:
            full_messages = [{"role": "system", "content": system_prompt}, *messages]
            with span.current():  # the SDK's HTTP request nests under the model-call span
                stream = await self._client.chat.completions.create(
                    model=self._model,
                    messages=full_messages,
                    stream=True,
                    stream_options={"include_usage": True},
                )
            async for chunk in stream:
                span.on_chunk(chunk)
                if chunk.usage is not None:
                    prompt_tokens = chunk.usage.prompt_tokens
                    completion_tokens = chunk.usage.completion_tokens
                if chunk.choices and chunk.choices[0].delta.content:
                    yield {"delta": chunk.choices[0].delta.content}
        except BaseException as exc:  # includes cancellation, which ends the span too
            status, error = "error", exc
            raise
        finally:
            span.end(input_tokens=prompt_tokens, output_tokens=completion_tokens, error=error)
            duration_ms = int((time.monotonic() - start) * 1000)
            # Not shielded: a client disconnect cancels this task, and the write is cancelled with it.
            await record_usage(
                tenant_id=tenant_id,
                model=self._model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=duration_ms,
                status=status,
            )

        yield {"done": True, "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


@lru_cache
def get_provider() -> ModelProvider:
    settings = get_settings()
    return OpenAICompatibleProvider(
        base_url=settings.model_base_url, api_key=settings.model_api_key, model=settings.model_name,
        provider_name=settings.model_provider_name, capture_content=settings.capture_message_content,
    )
