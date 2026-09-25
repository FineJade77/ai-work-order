import uuid

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.clients.tool_gateway import ToolCallFailed
from app.graph import nodes, prompts
from app.graph.builder import build_graph
from app.worker.runner import BUDGET_EXCEEDED, SUPERSEDED, TaskRunner
from tests.fakes import FakeModels, FakeTools, RecordingEvents, make_task

pytestmark = pytest.mark.anyio


class Env:
    def __init__(self, hits=None):
        self.models = FakeModels()
        self.tools = FakeTools(hits)
        self.events: list[tuple] = []
        self.conversation = uuid.uuid4()
        self.user = uuid.uuid4()
        self.seq = 0
        self.runner = TaskRunner(
            build_graph(InMemorySaver()), self.tools, self.models, lambda t: RecordingEvents(t, self.events)
        )

    def task(self, content):
        """The conversation's next turn, numbered like app-service numbers them."""
        self.seq += 1
        return make_task(self.conversation, content, user_id=self.user, seq=self.seq)

    def types(self, task=None):
        return [typ for tid, typ, _ in self.events if task is None or tid == str(task.task_id)]


@pytest.fixture
def env():
    return Env()


async def test_answer_waits_for_feedback_then_resolves(env):
    first = env.task("VPN 连不上")
    assert await env.runner.run(first) == "awaiting_feedback"
    types = env.types(first)
    assert types[0] == "retrieving" and types[-2:] == ["answer", "done"]
    assert "".join(d["delta"] for _, t, d in env.events if t == "answer_chunk") == env.models.answer

    env.models.resolution = "resolved"
    second = env.task("好了，谢谢")
    assert await env.runner.run(second) == "resolved"
    assert env.types(second) == ["reply", "done"]
    assert env.tools.names() == ["search_knowledge"]


async def test_unresolved_feedback_files_a_ticket_under_the_feedback_tasks_key(env):
    await env.runner.run(env.task("VPN 连不上"))
    feedback = env.task("还是不行")
    assert await env.runner.run(feedback) == "ticket_created"

    name, args, key = env.tools.calls[-1]
    assert name == "create_ticket"
    assert args == {"summary": env.models.summary, "category": "vpn"}
    assert key == f"{env.conversation}:{feedback.task_id}"
    created = next(d for _, t, d in env.events if t == "ticket_created")
    assert created["ticket_id"] == "T-1" and "T-1" in created["content"]


async def test_no_knowledge_hit_goes_straight_to_a_ticket():
    env = Env(hits=[])
    task = env.task("VPN 证书报错 0x80")
    assert await env.runner.run(task) == "ticket_created"
    assert env.types(task) == ["retrieving", "ticket_created", "done"]


@pytest.mark.parametrize(("intent", "category"), [("out_of_scope", "other"), ("in_scope", "other")])
async def test_out_of_scope_skips_search_and_marks_the_ticket(env, intent, category):
    env.models.intent, env.models.category = intent, category
    assert await env.runner.run(env.task("打印机卡纸")) == "ticket_created"
    assert env.tools.names() == ["create_ticket"]
    args = env.tools.calls[0][1]
    assert args["summary"].startswith(prompts.OUT_OF_SCOPE_MARKER) and args["category"] == "other"


async def test_explicit_ticket_request_is_not_marked_out_of_scope(env):
    env.models.intent, env.models.category = "ticket_request", "account"
    assert await env.runner.run(env.task("帮我直接提个工单，账号被锁了")) == "ticket_created"
    assert env.tools.calls[0][1] == {"summary": env.models.summary, "category": "account"}


@pytest.mark.parametrize("raw", [None, "我不太确定", '{"intent": "delete_everything"}'])
async def test_chitchat_or_unreadable_classification_asks_for_details(env, raw):
    env.models.intent = "chitchat"
    env.models.classify_raw = raw
    task = env.task("你好")
    assert await env.runner.run(task) == "replied"
    assert env.types(task) == ["reply", "done"]
    assert env.tools.calls == []


async def test_unreadable_resolution_is_treated_as_unresolved(env):
    await env.runner.run(env.task("VPN 连不上"))
    env.models.resolution = "maybe"
    assert await env.runner.run(env.task("嗯")) == "ticket_created"


async def test_new_question_as_feedback_starts_over(env):
    await env.runner.run(env.task("VPN 连不上"))
    env.models.resolution = "new_question"
    assert await env.runner.run(env.task("另外邮箱也收不到邮件")) == "awaiting_feedback"
    assert env.models.calls == ["classify", "answer", "resolution", "classify", "answer"]


async def test_a_new_run_after_a_ticket_starts_clean_but_keeps_history(env):
    env.models.intent = "ticket_request"
    await env.runner.run(env.task("提工单"))
    env.models.intent = "in_scope"
    assert await env.runner.run(env.task("VPN 连不上")) == "awaiting_feedback"
    state = (await env.runner._graph.aget_state({"configurable": {"thread_id": str(env.conversation)}})).values
    assert state["ticket_id"] is None and state["outcome"] is None
    assert [m["role"] for m in state["messages"]] == ["user", "assistant", "user", "assistant"]


async def test_crash_in_create_ticket_resumes_without_rewriting_the_summary():
    env = Env(hits=[])
    env.tools.crash_on_create = 1
    task = env.task("VPN 证书报错")
    with pytest.raises(RuntimeError):
        await env.runner.run(task)

    env.models.summary = "a different summary the second time"
    assert await env.runner.run(task) == "ticket_created"
    creates = [c for c in env.tools.calls if c[0] == "create_ticket"]
    assert len(creates) == 2 and creates[0] == creates[1]  # identical request, so the gateway replays it
    assert env.models.calls.count("summary") == 1
    assert "resumed" in env.types(task)


async def test_crash_mid_answer_resumes_at_the_answer(env):
    env.models.fail_answer_after_chunks = 1
    task = env.task("VPN 连不上")
    with pytest.raises(ConnectionError):
        await env.runner.run(task)
    assert await env.runner.run(task) == "awaiting_feedback"
    assert env.models.calls == ["classify", "answer", "answer"]
    assert env.tools.names() == ["search_knowledge"]
    types = env.types(task)
    assert types.index("resumed") < types.index("answer")


def _result(env, task):
    return next(d for tid, t, d in reversed(env.events) if tid == str(task.task_id) and t == "done")


async def test_each_run_reports_its_single_assistant_message_as_the_result(env):
    first = env.task("VPN 连不上")
    await env.runner.run(first)
    assert _result(env, first) == {"outcome": "awaiting_feedback", "content": env.models.answer, "ticket_id": None}

    second = env.task("还是不行")
    await env.runner.run(second)
    result = _result(env, second)
    assert result["ticket_id"] == "T-1" and result["content"] == prompts.TICKET_CREATED_REPLY.format(ticket_id="T-1")

    env.models.intent = "chitchat"
    third = env.task("谢谢")
    await env.runner.run(third)
    assert _result(env, third) == {"outcome": "replied", "content": prompts.CHITCHAT_REPLY, "ticket_id": None}


async def test_redelivered_finished_task_only_republishes_done(env):
    task = env.task("VPN 连不上")
    await env.runner.run(task)
    calls_before = (list(env.models.calls), list(env.tools.calls))
    assert await env.runner.run(task) == "awaiting_feedback"
    assert (env.models.calls, env.tools.calls) == calls_before
    assert env.types(task)[-2:] == ["done", "done"]


async def test_stale_copy_of_an_older_turn_is_not_taken_as_feedback(env):
    first = env.task("VPN 连不上")
    await env.runner.run(first)
    env.models.resolution, env.models.category = "new_question", "email"
    second = env.task("邮箱无法收发邮件")
    assert await env.runner.run(second) == "awaiting_feedback"
    config = {"configurable": {"thread_id": str(env.conversation)}}
    state_before = (await env.runner._graph.aget_state(config)).values
    seen = (list(env.events), list(env.models.calls), list(env.tools.calls))

    # Redelivered after turn 2 already ran, the thread waiting on turn 2's answer.
    assert await env.runner.run(first) == SUPERSEDED
    assert (env.events, env.models.calls, env.tools.calls) == seen
    assert (await env.runner._graph.aget_state(config)).values == state_before
    assert state_before["seq"] == second.seq


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://tool-gateway/v1/tools/call")
    return httpx.HTTPStatusError("gateway error", request=request, response=httpx.Response(status, request=request))


@pytest.fixture
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(nodes, "SEARCH_RETRY_DELAYS", (0, 0))


async def test_transient_search_failures_are_retried_then_answered(env, no_retry_pause):
    env.tools.search_failures = [ToolCallFailed("TOOL_ERROR", "knowledge-service down"), httpx.ConnectError("refused")]
    assert await env.runner.run(env.task("VPN 连不上")) == "awaiting_feedback"
    assert env.tools.names() == ["search_knowledge"] * 3


async def test_search_that_keeps_failing_gets_a_reply_instead_of_a_ticket(env, no_retry_pause):
    env.tools.search_failures = [_http_error(503)] * 3
    task = env.task("VPN 连不上")
    assert await env.runner.run(task) == "knowledge_unavailable"
    assert env.tools.names() == ["search_knowledge"] * 3
    assert "summary" not in env.models.calls
    assert env.types(task) == ["retrieving", "reply", "done"]
    assert _result(env, task) == {
        "outcome": "knowledge_unavailable", "content": prompts.KNOWLEDGE_UNAVAILABLE_REPLY, "ticket_id": None,
    }

    # Not left waiting for feedback: the next message is classified afresh, so asking for a ticket files one.
    env.models.intent = "ticket_request"
    assert await env.runner.run(env.task("提工单")) == "ticket_created"
    assert env.tools.names()[-1] == "create_ticket"


@pytest.mark.parametrize("failure", [ToolCallFailed("TOOL_NOT_ALLOWED", "no binding"), _http_error(401)])
async def test_misconfiguration_is_not_retried(env, no_retry_pause, failure):
    env.tools.search_failures = [failure]
    assert await env.runner.run(env.task("VPN 连不上")) == "knowledge_unavailable"
    assert env.tools.names() == ["search_knowledge"]


async def test_a_bug_in_the_search_call_is_not_mistaken_for_an_outage(env):
    env.tools.search_failures = [KeyError("results")]
    with pytest.raises(KeyError):
        await env.runner.run(env.task("VPN 连不上"))


async def test_budget_refused_at_classification_ends_the_turn_with_a_reply(env):
    env.models.over_budget = {"classify"}
    task = env.task("VPN 连不上")
    assert await env.runner.run(task) == BUDGET_EXCEEDED
    assert env.types(task) == ["reply", "done"]
    assert _result(env, task) == {
        "outcome": "budget_exceeded", "content": prompts.BUDGET_EXCEEDED_REPLY, "ticket_id": None,
    }
    assert env.tools.calls == []  # no search, and no ticket


async def test_budget_refused_mid_turn_does_not_leak_into_the_next_turn(env):
    env.models.over_budget = {"answer"}
    refused = env.task("VPN 连不上")
    assert await env.runner.run(refused) == BUDGET_EXCEEDED
    assert env.types(refused) == ["retrieving", "reply", "done"]

    # Budget back (next day): the new message starts afresh instead of resuming the refused answer.
    env.models.over_budget = set()
    env.models.calls.clear()
    assert await env.runner.run(env.task("邮箱无法收发邮件")) == "awaiting_feedback"
    assert env.models.calls == ["classify", "answer"]


async def test_budget_refused_while_checking_feedback(env):
    await env.runner.run(env.task("VPN 连不上"))
    env.models.over_budget = {"resolution"}
    assert await env.runner.run(env.task("还是不行")) == BUDGET_EXCEEDED
    assert "create_ticket" not in env.tools.names()
