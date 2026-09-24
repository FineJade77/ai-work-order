"""In-memory stand-ins for the gateways and the event stream, shared by unit and integration tests."""

import json
import uuid

from app.graph import prompts
from app.schemas.tasks import AgentTask

TENANT = uuid.UUID("e0e112f3-5afb-4229-869b-c0bd012324bb")  # demo tenant: has knowledge data and tool bindings
VPN_HIT = {"question": "VPN 连不上怎么办？", "answer": "重启 VPN 客户端后重新登录。", "score": 1.0}


def make_task(
    conversation_id: uuid.UUID, content: str, *, user_id: uuid.UUID | None = None, seq: int = 1
) -> AgentTask:
    return AgentTask(
        task_id=uuid.uuid4(),
        seq=seq,
        conversation_id=conversation_id,
        tenant_id=TENANT,
        user_id=user_id or uuid.uuid4(),
        content=content,
    )


class FakeModels:
    """Answers by prompt kind. Mutate the attributes between turns to script a conversation."""

    def __init__(self):
        self.intent = "in_scope"
        self.category = "vpn"
        self.classify_raw: str | None = None  # overrides the JSON built from intent/category
        self.resolution = "unresolved"
        self.answer = "请先重启 VPN 客户端，再重新登录。问题解决了吗？"
        self.summary = "员工 VPN 无法连接，已按知识库重启客户端，仍然失败。"
        self.fail_answer_after_chunks: int | None = None  # simulate a crash mid-stream, once
        self.calls: list[str] = []

    def _kind(self, system_prompt: str) -> str:
        for kind, prompt in (("classify", prompts.CLASSIFY), ("resolution", prompts.RESOLUTION), ("summary", prompts.SUMMARY)):
            if system_prompt == prompt:
                return kind
        return "answer"

    async def stream(self, task, *, system_prompt, messages):
        kind = self._kind(system_prompt)
        self.calls.append(kind)
        text = {
            "classify": self.classify_raw or json.dumps({"intent": self.intent, "category": self.category}),
            "resolution": f'好的：{{"resolution": "{self.resolution}"}}',
            "summary": self.summary,
            "answer": self.answer,
        }[kind]
        for i in range(0, len(text), 8):
            if kind == "answer" and self.fail_answer_after_chunks is not None and i // 8 == self.fail_answer_after_chunks:
                self.fail_answer_after_chunks = None
                raise ConnectionError("ai-gateway connection lost")
            yield text[i : i + 8]

    async def complete(self, task, *, system_prompt, messages):
        return "".join([d async for d in self.stream(task, system_prompt=system_prompt, messages=messages)])


class FakeTools:
    """Deduplicates create_ticket by idempotency key, like tool-gateway."""

    def __init__(self, hits: list[dict] | None = None):
        self.hits = [VPN_HIT] if hits is None else hits
        self.crash_on_create = 0  # raise this many times before creating, as if the worker died
        self.calls: list[tuple] = []
        self.tickets: dict[str, str] = {}

    async def call(self, task, tool_name, arguments, *, idempotency_key=None):
        self.calls.append((tool_name, arguments, idempotency_key))
        if tool_name == "search_knowledge":
            return {"results": self.hits}
        if self.crash_on_create:
            self.crash_on_create -= 1
            raise RuntimeError("worker crashed during create_ticket")
        return {"ticket_id": self.tickets.setdefault(idempotency_key, f"T-{len(self.tickets) + 1}")}

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]


class RecordingEvents:
    """Logs events as (task_id, type, data); a finished run logs 'done' with its result fields."""

    def __init__(self, task: AgentTask, log: list[tuple]):
        self._task_id = str(task.task_id)
        self._log = log

    async def publish(self, type_: str, **data) -> None:
        self._log.append((self._task_id, type_, data))

    async def finish(self, *, outcome: str, content: str, ticket_id: str | None) -> None:
        self._log.append((self._task_id, "done", {"outcome": outcome, "content": content, "ticket_id": ticket_id}))
