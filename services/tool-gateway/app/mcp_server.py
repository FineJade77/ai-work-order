import json

from mcp.server.mcpserver import MCPServer

from app.audit import audit_tool_call, record_rejected
from app.knowledge import search_knowledge_impl
from app.models import CreateTicketResult, KnowledgeResult, UpdateTicketStatusResult
from app.ticket_provider import ticket_provider
from app.validation import parse_tenant_id

mcp_server = MCPServer(name="tool-gateway")


@mcp_server.tool()
def search_knowledge(query: str, tenant_id: str) -> list[KnowledgeResult]:
    """Search this tenant's knowledge base for QA items matching the query."""
    params_summary = json.dumps({"query": query})
    try:
        parse_tenant_id(tenant_id)
    except ValueError as exc:
        record_rejected("search_knowledge", tenant_id, params_summary, str(exc))
        raise

    with audit_tool_call("search_knowledge", tenant_id, params_summary):
        return search_knowledge_impl(query=query, tenant_id=tenant_id)


@mcp_server.tool()
def create_ticket(summary: str, category: str, conversation_ref: str, tenant_id: str) -> CreateTicketResult:
    """Create an IT ticket for this tenant, linked to the source conversation."""
    params_summary = json.dumps({"summary": summary, "category": category, "conversation_ref": conversation_ref})
    try:
        parse_tenant_id(tenant_id)
    except ValueError as exc:
        record_rejected("create_ticket", tenant_id, params_summary, str(exc))
        raise

    with audit_tool_call("create_ticket", tenant_id, params_summary):
        ticket_id = ticket_provider.create(
            tenant_id=tenant_id, summary=summary, category=category, conversation_ref=conversation_ref
        )
        return CreateTicketResult(ticket_id=ticket_id)


@mcp_server.tool()
def update_ticket_status(ticket_id: str, status: str, tenant_id: str) -> UpdateTicketStatusResult:
    """Update an existing ticket's status.

    NOTE: implementation-plan.md's phase 2 spec lists this tool as (ticket_id, status) with no
    tenant_id. tenant_id is added here deliberately: RLS-based tenant isolation (see
    architecture-design.md section 4) needs a caller-declared tenant to scope the session
    against, and this tool otherwise has no way to do that. This keeps its access-control
    behavior consistent with search_knowledge and create_ticket instead of being a silent
    exception to the tenancy model.
    """
    params_summary = json.dumps({"ticket_id": ticket_id, "status": status})
    try:
        parse_tenant_id(tenant_id)
    except ValueError as exc:
        record_rejected("update_ticket_status", tenant_id, params_summary, str(exc))
        raise

    with audit_tool_call("update_ticket_status", tenant_id, params_summary):
        ok = ticket_provider.update_status(tenant_id=tenant_id, ticket_id=ticket_id, status=status)
        return UpdateTicketStatusResult(ok=ok)
