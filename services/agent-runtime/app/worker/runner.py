import logging
from collections.abc import Callable

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.clients.ai_gateway import AIGatewayClient
from app.clients.tool_gateway import ToolGatewayClient
from app.graph.state import RUN_FIELDS_RESET, Deps, Events
from app.schemas.tasks import AgentTask

logger = logging.getLogger("agent_runtime.runner")

# Upper bound on node executions per run: stops a classify → check_resolution loop that never ends.
RECURSION_LIMIT = 25


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
        task_id = str(task.task_id)

        if (snapshot.values or {}).get("task_id") == task_id:
            if snapshot.interrupts or not snapshot.next:
                # This task's run had already finished; only the acknowledgement was lost.
                return await self._finish(config, events)
            logger.info("resuming task_id=%s at %s", task_id, snapshot.next)
            await events.publish("resumed")
            graph_input = None
        elif snapshot.interrupts:
            # The previous run stopped at await_feedback; this message is the feedback.
            graph_input = Command(resume=task.content)
        else:
            graph_input = {"task_id": task_id, "messages": [{"role": "user", "content": task.content}], **RUN_FIELDS_RESET}

        deps = Deps(task=task, tools=self._tools, models=self._models, events=events)
        # durability="sync": each completed node is checkpointed before the next starts, so a
        # crash never re-runs a node that already finished (e.g. create_ticket after success).
        await self._graph.ainvoke(graph_input, config, context=deps, durability="sync")
        return await self._finish(config, events)

    async def _finish(self, config: dict, events: Events) -> str:
        snapshot = await self._graph.aget_state(config)
        outcome = "awaiting_feedback" if snapshot.interrupts else snapshot.values["outcome"]
        await events.publish("done", outcome=outcome)
        return outcome
