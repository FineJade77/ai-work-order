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

from app.clients.tool_gateway import ApprovalPending, ToolCallFailed, classify_failure
from app.graph import prompts
from app.graph.state import AgentState, Deps
from app.metrics import CLASSIFIED_INTENTS, KNOWLEDGE_SEARCHES
from app.tracing import event

logger = logging.getLogger("agent_runtime.graph")

HISTORY_WINDOW = 12  # messages sent to the model; older turns rarely matter for the current question
QUERY_MAX_CHARS = 200  # search_knowledge's schema limit
SUMMARY_MAX_CHARS = 500  # create_ticket's schema limit
# Pauses between up to three search attempts. Worst case, 3 x the 10s tool-gateway timeout plus
# these, stays well inside task_timeout_seconds (60s) together with classify_intent.
SEARCH_RETRY_DELAYS = (0.5, 1.0)

INTENTS = {"question", "action", "ticket_request", "chitchat"}
# Tools every tenant's agent uses on its fixed paths; the action path picks among the others.
BUILTIN_TOOLS = frozenset({"search_knowledge", "create_ticket"})
FINAL_APPROVAL_CODES = {"APPROVAL_REJECTED": "rejected", "APPROVAL_EXPIRED": "expired"}
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
    CLASSIFIED_INTENTS.add(1, {"app.intent": intent, "app.tenant_id": str(deps.task.tenant_id)})
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
            tenant = str(deps.task.tenant_id)
            KNOWLEDGE_SEARCHES.add(1, {"app.result": "hit" if hits else "miss", "app.tenant_id": tenant})
            return {"knowledge": hits, "knowledge_error": None, "category": _category_of(hits)}
        except Exception as exc:
            failure = classify_failure(exc)
            if failure is None:
                raise
        event("search_failed", code=failure.code, attempt=attempt, transient=failure.transient)
        if not failure.transient or delay is None:
            break
        logger.warning(
            "search_knowledge attempt %d failed task_id=%s code=%s; retrying", attempt, deps.task.task_id, failure.code
        )
        await anyio.sleep(delay)
    event("knowledge_unavailable", code=failure.code, attempts=attempt)
    KNOWLEDGE_SEARCHES.add(1, {"app.result": "unavailable", "app.tenant_id": str(deps.task.tenant_id)})
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


async def plan_action(state: AgentState, runtime: Runtime[Deps]) -> dict:
    """Has the model pick one of the tenant's action tools and its arguments from the conversation.
    What the tools are is tool-gateway's business (tools/list), so nothing here names a domain.
    The idempotency key is fixed here: this node's result is checkpointed, so the call made after
    an approval (in a later run, under another task) reuses it and the tool runs at most once."""
    deps = runtime.context
    key = f"{deps.task.conversation_id}:{deps.task.task_id}:action"
    try:
        tools = [t["function"] for t in await deps.tools.list_tools(deps.task)]
    except Exception as exc:
        if classify_failure(exc) is None:
            raise
        event("action_tools_unavailable", error=type(exc).__name__)
        return {"action_tool": None, "action_key": key}
    tools = [t for t in tools if t["name"] not in BUILTIN_TOOLS]
    if not tools:
        event("no_action_tools")
        return {"action_tool": None, "action_key": key}
    system_prompt = prompts.PLAN_ACTION.replace("{tools}", json.dumps(tools, ensure_ascii=False))
    plan = _parse_json(await deps.models.complete(deps.task, system_prompt=system_prompt, messages=_history(state)))
    tool, arguments = plan.get("tool"), plan.get("arguments")
    if tool not in {t["name"] for t in tools} or not isinstance(arguments, dict):
        # No fitting tool, missing information, or unreadable output: a person takes it from here.
        event("no_action_planned", tool=tool)
        return {"action_tool": None, "action_key": key}
    return {"action_tool": tool, "action_arguments": arguments, "action_key": key}


async def execute_action(state: AgentState, runtime: Runtime[Deps]) -> dict:
    """Calls the planned tool. If the tenant's policy makes it wait for a handler, tells the
    employee and hands over to await_approval; once resumed, runs again with the same key and
    gets the tool's result, or the rejection or expiry, back from tool-gateway."""
    deps = runtime.context
    try:
        result = await deps.tools.call(
            deps.task, state["action_tool"], state["action_arguments"], idempotency_key=state["action_key"]
        )
    except ApprovalPending as pending:
        event("approval_pending", approval_id=pending.approval_id)
        await deps.events.publish("reply", content=prompts.ACTION_SUBMITTED_REPLY)
        return {
            "action_status": "pending",
            "approval_id": pending.approval_id,
            "messages": [{"role": "assistant", "content": prompts.ACTION_SUBMITTED_REPLY}],
        }
    except ToolCallFailed as exc:
        if exc.code in FINAL_APPROVAL_CODES:  # the approver's note came with the decision (await_approval)
            return {"action_status": FINAL_APPROVAL_CODES[exc.code]}
        event("action_failed", code=exc.code)
        return {"action_status": "failed"}  # a person takes over: summarize_ticket
    event("action_done", tool=state["action_tool"])
    return {"action_status": "done"}


async def await_approval(state: AgentState, runtime: Runtime[Deps]) -> dict:
    # Ends this run. An approval task (app-service turns the decision event into one) resumes
    # here; employee messages meanwhile get a status reply from the runner and do not resume.
    # Nothing may come before interrupt(): the node runs again from the top when resumed.
    decision = interrupt({"awaiting": "approval", "approval_id": state["approval_id"]})
    task = runtime.context.task
    event("approval_decided", decision=decision.get("decision"))
    return {"task_id": str(task.task_id), "seq": task.seq, "approval_note": decision.get("note") or None}


async def report_action(state: AgentState, runtime: Runtime[Deps]) -> dict:
    status = state["action_status"]
    if status == "done":
        content = prompts.ACTION_APPROVED_REPLY if state.get("approval_id") else prompts.ACTION_DONE_REPLY
    elif status == "rejected":
        note = state.get("approval_note")
        content = prompts.ACTION_REJECTED_REPLY.format(note=f"审批意见：{note}。" if note else "")
    else:
        content = prompts.ACTION_EXPIRED_REPLY
    await runtime.context.events.publish("reply", content=content)
    return {"outcome": f"action_{status}", "messages": [{"role": "assistant", "content": content}]}


def route_after_classify(state: AgentState) -> str:
    return {
        "question": "retrieve_knowledge",
        "action": "plan_action",
        "chitchat": "reply",
    }.get(state["intent"], "summarize_ticket")  # ticket_request


def route_after_plan(state: AgentState) -> str:
    return "execute_action" if state.get("action_tool") else "summarize_ticket"


def route_after_execute(state: AgentState) -> str:
    return {"pending": "await_approval", "failed": "summarize_ticket"}.get(state["action_status"], "report_action")


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
