"""Graph nodes. Each may be re-run after a crash (the checkpoint only records completed nodes),
so anything with a side effect must be idempotent: the ticket is created under a key derived
from the task, and its LLM-written summary is produced by a separate, earlier node so a re-run
sends the identical request."""

import json
import logging
import re

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.clients.tool_gateway import ToolCallFailed
from app.graph import prompts
from app.graph.state import AgentState, Deps

logger = logging.getLogger("agent_runtime.graph")

HISTORY_WINDOW = 12  # messages sent to the model; older turns rarely matter for an IT question
QUERY_MAX_CHARS = 200  # search_knowledge's schema limit
SUMMARY_MAX_CHARS = 500  # create_ticket's schema limit

INTENTS = {"in_scope", "out_of_scope", "ticket_request", "chitchat"}
CATEGORIES = {"vpn", "account", "email", "other"}
RESOLUTIONS = {"resolved", "unresolved", "new_question"}

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _history(state: AgentState) -> list[dict]:
    return state.get("messages", [])[-HISTORY_WINDOW:]


def _last_user_message(state: AgentState) -> str:
    return next(m["content"] for m in reversed(state["messages"]) if m["role"] == "user")


def _parse_json(text: str) -> dict:
    """Models sometimes wrap JSON in prose or code fences; take the outermost object."""
    match = _JSON_OBJECT.search(text)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


async def classify_intent(state: AgentState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    raw = await deps.models.complete(deps.task, system_prompt=prompts.CLASSIFY, messages=_history(state))
    parsed = _parse_json(raw)
    intent, category = parsed.get("intent"), parsed.get("category")
    if intent not in INTENTS:
        # Unreadable output: ask the employee to describe the problem rather than guess.
        logger.warning("unparseable classification task_id=%s output=%r", deps.task.task_id, raw[:200])
        intent = "chitchat"
    if category not in CATEGORIES:
        category = "other"
    if intent == "in_scope" and category == "other":
        intent = "out_of_scope"
    return {"intent": intent, "category": category}


async def retrieve_knowledge(state: AgentState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    await deps.events.publish("retrieving")
    query = _last_user_message(state)[:QUERY_MAX_CHARS]
    try:
        result = await deps.tools.call(deps.task, "search_knowledge", {"query": query})
    except ToolCallFailed as exc:
        # A failed search means no answer we can stand behind; the ticket path still helps.
        logger.warning("search_knowledge failed task_id=%s: %s", deps.task.task_id, exc)
        return {"knowledge": []}
    return {"knowledge": result["results"]}


async def generate_answer(state: AgentState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    knowledge = "\n\n".join(f"问：{k['question']}\n答：{k['answer']}" for k in state["knowledge"])
    parts: list[str] = []
    async for delta in deps.models.stream(
        deps.task, system_prompt=prompts.ANSWER.format(knowledge=knowledge), messages=_history(state)
    ):
        parts.append(delta)
        await deps.events.publish("answer_chunk", delta=delta)
    answer = "".join(parts)
    await deps.events.publish("answer", content=answer)
    return {"messages": [{"role": "assistant", "content": answer}]}


async def await_feedback(state: AgentState, runtime: Runtime[Deps]) -> dict:
    # Ends this run. The conversation's next message resumes here as the interrupt's value, and
    # this node then runs again from the top, so nothing may come before interrupt().
    reply = interrupt({"awaiting": "feedback"})
    return {"task_id": str(runtime.context.task.task_id), "messages": [{"role": "user", "content": reply}]}


async def check_resolution(state: AgentState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    raw = await deps.models.complete(deps.task, system_prompt=prompts.RESOLUTION, messages=_history(state))
    resolution = _parse_json(raw).get("resolution")
    if resolution not in RESOLUTIONS:
        # Unreadable output: a needless ticket costs less than leaving the employee stuck.
        logger.warning("unparseable resolution task_id=%s output=%r", deps.task.task_id, raw[:200])
        resolution = "unresolved"
    if resolution != "resolved":
        return {"resolution": resolution}
    await deps.events.publish("reply", content=prompts.RESOLVED_REPLY)
    return {
        "resolution": resolution,
        "outcome": "resolved",
        "messages": [{"role": "assistant", "content": prompts.RESOLVED_REPLY}],
    }


async def reply(state: AgentState, runtime: Runtime[Deps]) -> dict:
    await runtime.context.events.publish("reply", content=prompts.CHITCHAT_REPLY)
    return {"outcome": "replied", "messages": [{"role": "assistant", "content": prompts.CHITCHAT_REPLY}]}


async def summarize_ticket(state: AgentState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    summary = (await deps.models.complete(deps.task, system_prompt=prompts.SUMMARY, messages=_history(state))).strip()
    if not summary:
        summary = _last_user_message(state)
    if state.get("intent") == "out_of_scope":
        summary = prompts.OUT_OF_SCOPE_MARKER + summary
    return {"ticket_summary": summary[:SUMMARY_MAX_CHARS]}


async def create_ticket(state: AgentState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    # Stable across re-runs of this task, so a retry replays the original call instead of
    # filing a second ticket (tool-gateway and ticket-service both deduplicate on it).
    key = f"{deps.task.conversation_id}:{deps.task.task_id}"
    result = await deps.tools.call(
        deps.task,
        "create_ticket",
        {"summary": state["ticket_summary"], "category": state.get("category") or "other"},
        idempotency_key=key,
    )
    ticket_id = result["ticket_id"]
    content = prompts.TICKET_CREATED_REPLY.format(ticket_id=ticket_id)
    await deps.events.publish("ticket_created", ticket_id=ticket_id, content=content)
    return {"ticket_id": ticket_id, "outcome": "ticket_created", "messages": [{"role": "assistant", "content": content}]}


def route_after_classify(state: AgentState) -> str:
    return {
        "in_scope": "retrieve_knowledge",
        "chitchat": "reply",
    }.get(state["intent"], "summarize_ticket")  # out_of_scope, ticket_request


def route_after_retrieve(state: AgentState) -> str:
    # knowledge-service already drops matches below its relevance threshold.
    return "generate_answer" if state["knowledge"] else "summarize_ticket"


def route_after_resolution(state: AgentState) -> str:
    return {
        "resolved": "__end__",
        "new_question": "classify_intent",
    }.get(state["resolution"], "summarize_ticket")
