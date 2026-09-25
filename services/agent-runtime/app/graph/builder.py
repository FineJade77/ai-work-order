from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graph import nodes
from app.graph.state import AgentState, Deps


def build_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """
    classify_intent ─┬─ chitchat ─────────────────────────────→ reply → END
                     ├─ ticket_request ───────────────────────→ summarize_ticket → create_ticket → END
                     └─ question → retrieve_knowledge ─┬─ no hits → summarize_ticket
                                                       ├─ search failed → knowledge_unavailable → END
                                                       └─ hits → generate_answer → await_feedback (interrupt)
    await_feedback → check_resolution ─┬─ resolved → END
                                       ├─ unresolved → summarize_ticket
                                       └─ new_question → classify_intent
    """
    graph = StateGraph(AgentState, context_schema=Deps)
    for node in (
        nodes.classify_intent,
        nodes.retrieve_knowledge,
        nodes.generate_answer,
        nodes.await_feedback,
        nodes.check_resolution,
        nodes.reply,
        nodes.knowledge_unavailable,
        nodes.summarize_ticket,
        nodes.create_ticket,
    ):
        graph.add_node(node.__name__, node)

    graph.add_edge(START, "classify_intent")
    graph.add_conditional_edges(
        "classify_intent", nodes.route_after_classify, ["retrieve_knowledge", "reply", "summarize_ticket"]
    )
    graph.add_conditional_edges(
        "retrieve_knowledge", nodes.route_after_retrieve, ["generate_answer", "summarize_ticket", "knowledge_unavailable"]
    )
    graph.add_edge("generate_answer", "await_feedback")
    graph.add_edge("await_feedback", "check_resolution")
    graph.add_conditional_edges(
        "check_resolution", nodes.route_after_resolution, [END, "classify_intent", "summarize_ticket"]
    )
    graph.add_edge("summarize_ticket", "create_ticket")
    graph.add_edge("create_ticket", END)
    graph.add_edge("reply", END)
    graph.add_edge("knowledge_unavailable", END)
    return graph.compile(checkpointer=checkpointer)
