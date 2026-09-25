"""Graph nodes. Each may be re-run after a crash (the checkpoint only records completed nodes),
so anything with a side effect must be idempotent: the ticket is created under a key derived
from the task, and its LLM-written summary is produced by a separate, earlier node so a re-run
sends the identical request."""

import json
import logging
import re

import anyio
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.clients.tool_gateway import classify_failure
from app.graph import prompts
from app.graph.state import AgentState, Deps

logger = logging.getLogger("agent_runtime.graph")

HISTORY_WINDOW = 12  # messages sent to the model; older turns rarely matter for the current question
QUERY_MAX_CHARS = 200  # search_knowledge's schema limit
SUMMARY_MAX_CHARS = 500  # create_ticket's schema limit
# Pauses between up to three search attempts. Worst case, 3 x the 10s tool-gateway timeout plus
# these, stays well inside task_timeout_seconds (60s) together with classify_intent.
SEARCH_RETRY_DELAYS = (0.5, 1.0)

INTENTS = {"question", "ticket_request", "chitchat"}
# A ticket's category when no knowledge entry was used (tool-gateway caps categories at 50 chars).
FALLBACK_CATEGORY = "other"
CATEGORY_MAX_CHARS = 50
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
    intent = _parse_json(raw).get("intent")
    if intent not in INTENTS:
        # Unreadable output: ask the employee to describe the problem rather than guess.
        logger.warning("unparseable classification task_id=%s output=%r", deps.task.task_id, raw[:200])
        intent = "chitchat"
    # A new question within the same run (check_resolution -> new_question) must not inherit the
    # previous question's search results or the category taken from them.
    return {"intent": intent, "category": None, "knowledge": None, "knowledge_error": None}


async def retrieve_knowledge(state: AgentState, runtime: Runtime[Deps]) -> dict:
    """A failed search is not "no hits": filing a ticket for it would turn an outage (or a tenant
    missing its tool bindings) into a ticket for every question. Transient failures are retried
    briefly; if the search still fails, knowledge_error routes to knowledge_unavailable."""
    deps = runtime.context
    await deps.events.publish("retrieving")
    query = _last_user_message(state)[:QUERY_MAX_CHARS]
    for attempt, delay in enumerate((*SEARCH_RETRY_DELAYS, None), start=1):
        try:
            result = await deps.tools.call(deps.task, "search_knowledge", {"query": query})
            hits = result["results"]
            return {"knowledge": hits, "knowledge_error": None, "category": _category_of(hits)}
        except Exception as exc:
            failure = classify_failure(exc)
            if failure is None:
                raise
        if not failure.transient or delay is None:
            break
        logger.warning(
            "search_knowledge attempt %d failed task_id=%s code=%s; retrying", attempt, deps.task.task_id, failure.code
        )
        await anyio.sleep(delay)
    # Fixed format, so log-based alerting can match on the leading keyword.
    logger.error(
        "knowledge_unavailable tenant_id=%s task_id=%s code=%s attempts=%d",
        deps.task.tenant_id, deps.task.task_id, failure.code, attempt,
    )
    return {"knowledge": None, "knowledge_error": failure.code, "category": None}


def _category_of(hits: list[dict]) -> str | None:
    """The ticket category, should this question end in one: that of the best-matching entry.
    Categories are the tenant's own labels (vpn, leave, expense...), so nothing is checked
    against a list; a missing or oversized one falls back rather than fail the ticket later."""
    category = hits[0].get("category") if hits else None
    if isinstance(category, str) and 0 < len(category) <= CATEGORY_MAX_CHARS:
        return category
    return None


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
    task = runtime.context.task
    return {"task_id": str(task.task_id), "seq": task.seq, "messages": [{"role": "user", "content": reply}]}


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


async def knowledge_unavailable(state: AgentState, runtime: Runtime[Deps]) -> dict:
    # Ends the run without waiting for feedback: the employee's next message is classified afresh,
    # so asking again searches again and asking for a ticket files one.
    await runtime.context.events.publish("reply", content=prompts.KNOWLEDGE_UNAVAILABLE_REPLY)
    return {
        "outcome": "knowledge_unavailable",
        "messages": [{"role": "assistant", "content": prompts.KNOWLEDGE_UNAVAILABLE_REPLY}],
    }


async def reply(state: AgentState, runtime: Runtime[Deps]) -> dict:
    await runtime.context.events.publish("reply", content=prompts.CHITCHAT_REPLY)
    return {"outcome": "replied", "messages": [{"role": "assistant", "content": prompts.CHITCHAT_REPLY}]}


async def summarize_ticket(state: AgentState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    summary = (await deps.models.complete(deps.task, system_prompt=prompts.SUMMARY, messages=_history(state))).strip()
    if not summary:
        summary = _last_user_message(state)
    if state.get("knowledge") == []:  # searched, nothing found (not: never searched, or search down)
        summary = prompts.NO_KNOWLEDGE_MARKER + summary
    return {"ticket_summary": summary[:SUMMARY_MAX_CHARS]}


async def create_ticket(state: AgentState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    # Stable across re-runs of this task, so a retry replays the original call instead of
    # filing a second ticket (tool-gateway and ticket-service both deduplicate on it).
    key = f"{deps.task.conversation_id}:{deps.task.task_id}"
    result = await deps.tools.call(
        deps.task,
        "create_ticket",
        {"summary": state["ticket_summary"], "category": state.get("category") or FALLBACK_CATEGORY},
        idempotency_key=key,
    )
    ticket_id = result["ticket_id"]
    content = prompts.TICKET_CREATED_REPLY.format(ticket_id=ticket_id)
    await deps.events.publish("ticket_created", ticket_id=ticket_id, content=content)
    return {"ticket_id": ticket_id, "outcome": "ticket_created", "messages": [{"role": "assistant", "content": content}]}


def route_after_classify(state: AgentState) -> str:
    return {
        "question": "retrieve_knowledge",
        "chitchat": "reply",
    }.get(state["intent"], "summarize_ticket")  # ticket_request


def route_after_retrieve(state: AgentState) -> str:
    if state.get("knowledge_error"):
        return "knowledge_unavailable"
    # knowledge-service already drops matches below its relevance threshold.
    return "generate_answer" if state["knowledge"] else "summarize_ticket"


def route_after_resolution(state: AgentState) -> str:
    return {
        "resolved": "__end__",
        "new_question": "classify_intent",
    }.get(state["resolution"], "summarize_ticket")
