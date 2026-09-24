import uuid

import pytest

from app.core.errors import ApiError
from app.core.security import CurrentUser
from app.services.ticket_access import TicketAccess

pytestmark = pytest.mark.anyio

TENANT = uuid.uuid4()
EMPLOYEE = CurrentUser(id=uuid.uuid4(), tenant_id=TENANT, role="employee")
IT = CurrentUser(id=uuid.uuid4(), tenant_id=TENANT, role="it_staff")
TICKET = uuid.uuid4()


class FakeTickets:
    def __init__(self, requester):
        self.requester = requester
        self.calls = []

    async def list(self, tenant_id, **filters):
        self.calls.append(("list", tenant_id, filters))
        return {"items": [], "next_cursor": None}

    async def get(self, tenant_id, ticket_id):
        self.calls.append(("get", tenant_id, ticket_id))
        return {"ticket": {"id": str(ticket_id), "requester_id": str(self.requester)}, "events": []}

    async def transition(self, tenant_id, ticket_id, body):
        self.calls.append(("transition", tenant_id, body))
        return {"id": str(ticket_id)}


async def test_employee_listing_is_forced_to_their_own_tickets():
    fake = FakeTickets(EMPLOYEE.id)
    other = uuid.uuid4()
    await TicketAccess(fake).list(EMPLOYEE, status="open", assignee_id=other, requester_id=other, limit=5, cursor=None)
    _, tenant, filters = fake.calls[0]
    assert tenant == TENANT
    assert (filters["requester_id"], filters["assignee_id"], filters["status"]) == (EMPLOYEE.id, None, "open")


async def test_it_staff_filters_pass_through():
    fake = FakeTickets(EMPLOYEE.id)
    await TicketAccess(fake).list(IT, status=None, assignee_id=IT.id, requester_id=None, limit=5, cursor="c")
    assert fake.calls[0][2] == {"status": None, "assignee_id": IT.id, "requester_id": None, "limit": 5, "cursor": "c"}


async def test_employee_cannot_see_someone_elses_ticket():
    with pytest.raises(ApiError) as exc:
        await TicketAccess(FakeTickets(uuid.uuid4())).get(EMPLOYEE, TICKET)
    assert (exc.value.http_status, exc.value.code) == (404, "TICKET_NOT_FOUND")


async def test_it_staff_sees_any_ticket_in_the_tenant():
    detail = await TicketAccess(FakeTickets(uuid.uuid4())).get(IT, TICKET)
    assert detail["ticket"]["id"] == str(TICKET)


@pytest.mark.parametrize("action", ["start", "resolve"])
async def test_employee_cannot_take_it_actions(action):
    fake = FakeTickets(EMPLOYEE.id)
    with pytest.raises(ApiError) as exc:
        await TicketAccess(fake).transition(EMPLOYEE, TICKET, action=action, expected_version=1, note="x")
    assert (exc.value.http_status, exc.value.code) == (403, "NOT_IT_STAFF")
    assert fake.calls == []


async def test_employee_action_on_someone_elses_ticket_is_404_before_any_change():
    fake = FakeTickets(uuid.uuid4())
    with pytest.raises(ApiError) as exc:
        await TicketAccess(fake).transition(EMPLOYEE, TICKET, action="cancel", expected_version=1, note=None)
    assert exc.value.http_status == 404
    assert [c[0] for c in fake.calls] == ["get"]


async def test_start_assigns_the_caller_and_the_actor_is_always_the_caller():
    fake = FakeTickets(EMPLOYEE.id)
    await TicketAccess(fake).transition(IT, TICKET, action="start", expected_version=3, note=None)
    body = fake.calls[-1][2]
    assert body == {"action": "start", "actor_id": IT.id, "expected_version": 3, "note": None, "assignee_id": IT.id}
