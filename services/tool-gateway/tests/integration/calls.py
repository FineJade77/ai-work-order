from tests.integration.env import ROLE


def make_call(client, t):
    def _call(tool, args, *, key=None, role=ROLE, user=None):
        body = {
            "tenant_id": t.id,
            "session_id": t.session,
            "user_id": user or t.user,
            "agent_role": role,
            "tool_name": tool,
            "arguments": args,
        }
        return client.post("/v1/tools/call", json=body, headers={"Idempotency-Key": key} if key else {})

    return _call
