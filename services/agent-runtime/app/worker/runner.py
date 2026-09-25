import logging
from collections.abc import Callable

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.clients.ai_gateway import AIGatewayClient, BudgetExceeded
from app.clients.tool_gateway import ToolGatewayClient
from app.graph import prompts
from app.graph.state import RUN_FIELDS_RESET, Deps, Events
from app.schemas.tasks import AgentTask
from app.tracing import event

logger = logging.getLogger("agent_runtime.runner")

# Upper bound on node executions per run: stops a classify → check_resolution loop that never ends.
RECURSION_LIMIT = 25
# run()'s return for a stale task that was skipped; not an outcome, so never published.
SUPERSEDED = "superseded"
BUDGET_EXCEEDED = "budget_exceeded"


class TaskRunner:
    def __init__(
        self,
        graph: CompiledStateGraph,
        tools: ToolGatewayClient,
        models: AIGatewayClient,
        events_for: Callable[[AgentTask], Events],
    ):
        self._graph = graph
        self._tools = tools
        self._models = models
        self._events_for = events_for

    async def run(self, task: AgentTask) -> str:
        """Advances the conversation's graph with one user message and returns the outcome.

        Safe to call again for the same task after a crash: it continues from the last
        completed node instead of starting over.
        """
        events = self._events_for(task)
        config = {"configurable": {"thread_id": str(task.conversation_id)}, "recursion_limit": RECURSION_LIMIT}
        snapshot = await self._graph.aget_state(config)
        values = snapshot.values or {}
        task_id = str(task.task_id)

        # Threads checkpointed before seq existed have none; their next task sets it.
        if values.get("seq") is not None and task.seq < values["seq"]:
            # A copy of a turn the thread has moved past. Run it and, if the thread is waiting for
            # feedback, its old question would be taken as the answer to a newer one. Its turn was
            # closed in app-service long ago, so there is nothing to report either.
            event("skipped_stale", check="checkpoint seq", thread_seq=values["seq"])
            logger.warning("skipping stale task_id=%s seq=%d: thread is at seq %d", task_id, task.seq, values["seq"])
            return SUPERSEDED

        if values.get("task_id") == task_id:
            if snapshot.interrupts or not snapshot.next:
                # This task's run had already finished; only the acknowledgement was lost.
                event("already_finished")
                return await self._finish(config, events)
            event("resumed", at=",".join(snapshot.next))
            logger.info("resuming task_id=%s at %s", task_id, snapshot.next)
            await events.publish("resumed")
            graph_input = None
        elif snapshot.interrupts:
            # The previous run stopped at await_feedback; this message is the feedback.
            graph_input = Command(resume=task.content)
        else:
            graph_input = {
                "task_id": task_id,
                "seq": task.seq,
                "messages": [{"role": "user", "content": task.content}],
                **RUN_FIELDS_RESET,
            }

        deps = Deps(task=task, tools=self._tools, models=self._models, events=events)
        try:
            # durability="sync": each completed node is checkpointed before the next starts, so a
            # crash never re-runs a node that already finished (e.g. create_ticket after success).
            await self._graph.ainvoke(graph_input, config, context=deps, durability="sync")
        except BudgetExceeded:
            return await self._refuse_over_budget(task, events)
        return await self._finish(config, events)

    async def _refuse_over_budget(self, task: AgentTask, events: Events) -> str:
        """Ends the turn with a fixed reply instead of retrying: until the budget day rolls over,
        every model call would be refused again. No ticket either: that needs the model for its
        summary, and filing tickets on a budget outage would flood the handlers' queue.

        The thread is left at the node that was refused. The conversation's next message is a
        new graph input, which discards that pending node, so nothing resumes it by accident.
        The reply is therefore not in the thread's history, only in app-service's."""
        # Fixed format, so log-based alerting can match on the leading keyword.
        event("budget_exceeded")
        logger.error("budget_exceeded tenant_id=%s task_id=%s", task.tenant_id, task.task_id)
        await events.publish("reply", content=prompts.BUDGET_EXCEEDED_REPLY)
        await events.finish(outcome=BUDGET_EXCEEDED, content=prompts.BUDGET_EXCEEDED_REPLY, ticket_id=None)
        return BUDGET_EXCEEDED

    async def _finish(self, config: dict, events: Events) -> str:
        snapshot = await self._graph.aget_state(config)
        values = snapshot.values
        outcome = "awaiting_feedback" if snapshot.interrupts else values["outcome"]
        # Every run ends with exactly one assistant message: the answer, a reply, or the ticket notice.
        content = next(m["content"] for m in reversed(values["messages"]) if m["role"] == "assistant")
        await events.finish(outcome=outcome, content=content, ticket_id=values.get("ticket_id"))
        return outcome
