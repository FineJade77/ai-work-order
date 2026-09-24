import json
from collections.abc import AsyncIterator

import httpx

from app.schemas.tasks import AgentTask


class ModelStreamIncomplete(Exception):
    """The stream ended without its final 'done' event, i.e. the upstream call failed midway."""


class AIGatewayClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def stream(self, task: AgentTask, *, system_prompt: str, messages: list[dict]) -> AsyncIterator[str]:
        """Yields answer text deltas from ai-gateway's text/event-stream response."""
        body = {"tenant_id": str(task.tenant_id), "system_prompt": system_prompt, "messages": messages}
        async with self._http.stream("POST", "/v1/generate", json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: ") :])
                if "delta" in event:
                    yield event["delta"]
                elif event.get("done"):
                    return
        raise ModelStreamIncomplete("ai-gateway stream ended before its done event")

    async def complete(self, task: AgentTask, *, system_prompt: str, messages: list[dict]) -> str:
        return "".join([d async for d in self.stream(task, system_prompt=system_prompt, messages=messages)])
