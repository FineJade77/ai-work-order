#!/usr/bin/env bash
# Plays one scenario through app-service as an employee, printing the agent's streamed events.
# Needs curl and jq, and the stack from `docker compose up -d --build`.
#
#   scripts/demo.sh vpn        answer, "still broken" -> ticket
#   scripts/demo.sh account    answer, "fixed, thanks" -> resolved, no ticket
#   scripts/demo.sh email      answer, "still broken" -> ticket
#   scripts/demo.sh printer    nothing in the knowledge base -> ticket straight away
#   scripts/demo.sh leave      the northwind tenant, an HR/admin desk: answer, "still rejected" -> ticket
#                              filed under northwind's own category (leave), not an IT one
#   scripts/demo.sh access     HITL: asks for system access -> the agent's grant_access waits for approval ->
#                              the handler (it.staff) approves it -> the paused run resumes and grants it.
#                              DECISION=reject to see a refusal instead
#
# The model decides the replies, so wording (and occasionally the path) varies between runs.
set -euo pipefail

BASE="${BASE:-http://localhost:8001/v1}"
if [ "${1:-}" = leave ]; then  # northwind's knowledge base is HR/admin, not IT
  TENANT="${TENANT:-northwind}"
  EMAIL="${EMAIL:-demo.user@northwind.example}"
fi
TENANT="${TENANT:-demo}"
EMAIL="${EMAIL:-demo.user@example.com}"
PASSWORD="${PASSWORD:-demo-password}"

case "${1:-}" in
  vpn)     MESSAGES=("VPN 连不上" "按你说的试了，还是连不上") ;;
  account) MESSAGES=("账号被锁定了，登录不了" "好了，可以登录了，谢谢") ;;
  email)   MESSAGES=("邮箱收不到邮件" "照着做了，还是收不到") ;;
  printer) MESSAGES=("打印机无法打印") ;;
  leave)   MESSAGES=("年假怎么申请" "按流程提交了，还是没通过") ;;
  access)  MESSAGES=("帮我开通财务系统的只读权限，月底对账要看报表") ;;
  *) echo "usage: $0 vpn|account|email|printer|leave|access" >&2; exit 2 ;;
esac

login() {
  curl -sf "$BASE/auth/login" -H 'content-type: application/json' \
    -d "$(jq -n --arg t "$TENANT" --arg e "$1" --arg p "$PASSWORD" '{tenant: $t, email: $e, password: $p}')" \
    | jq -r .access_token
}
AUTH="Authorization: Bearer $(login "$EMAIL")"
CONVERSATION="$(curl -sf -X POST "$BASE/conversations" -H "$AUTH" | jq -r .id)"
echo "conversation $CONVERSATION"

# Sends one message and prints its events until done/error.
say() {
  echo; echo "员工> $1"
  local task
  task="$(curl -sf "$BASE/conversations/$CONVERSATION/messages" -H "$AUTH" -H 'content-type: application/json' \
    -d "$(jq -n --arg c "$1" '{content: $c}')" | jq -r .task_id)"
  local event=""
  while IFS= read -r line; do
    case "$line" in
      "event: "*) event="${line#event: }" ;;
      "data: "*)
        data="${line#data: }"
        case "$event" in
          retrieving)     echo "  [检索知识库]" ;;
          answer_chunk)   printf '%s' "$(jq -r .delta <<<"$data")" ;;
          answer)         echo ;;
          reply)          echo "助手> $(jq -r .content <<<"$data")" ;;
          ticket_created) echo "助手> $(jq -r .content <<<"$data")" ;;
          resumed)        echo "  [处理中断后恢复，重新生成]" ;;
          error)          echo "  [失败] $(jq -r .message <<<"$data")" ;;
          done)           echo "  [本轮结束: $(jq -r .outcome <<<"$data")]" ;;
        esac ;;
    esac
  done < <(curl -sfN "$BASE/conversations/$CONVERSATION/stream?task_id=$task" -H "$AUTH")
}

for message in "${MESSAGES[@]}"; do
  say "$message"
done

# The handler's side of HITL: find this conversation's pending approval and decide it. The decision
# reaches the agent as an event (tool-gateway outbox -> approval_events -> app-service -> agent_tasks),
# so there is no stream to follow here; poll the history for the resumed run's reply instead.
if [ "$1" = access ]; then
  HANDLER="Authorization: Bearer $(login "${HANDLER_EMAIL:-it.staff@example.com}")"
  approval=""
  for _ in $(seq 20); do
    approval="$(curl -sf "$BASE/approvals" -H "$HANDLER" \
      | jq -c --arg c "$CONVERSATION" '[.items[] | select(.conversation_id == $c)][0] // empty')"
    [ -n "$approval" ] && break
    sleep 1
  done
  if [ -z "$approval" ]; then
    echo; echo "没有待审批：模型这次没有调用 grant_access（回复措辞与路径因模型而异，可以再跑一次）"; exit 1
  fi
  id="$(jq -r .id <<<"$approval")"
  echo; echo "处理人> 待审批 $id: $(jq -c '{tool: .tool_name, arguments}' <<<"$approval")"
  decision="${DECISION:-approve}"
  seen="$(curl -sf "$BASE/conversations/$CONVERSATION/messages" -H "$AUTH" | jq '.messages | length')"
  curl -sf -X POST "$BASE/approvals/$id/$decision" -H "$HANDLER" -H 'content-type: application/json' \
    -d '{"note": "已与部门经理核实"}' | jq -r '"处理人> \(.status)（\(.note)）"'
  for _ in $(seq 60); do
    history="$(curl -sf "$BASE/conversations/$CONVERSATION/messages" -H "$AUTH")"
    if [ "$(jq '.messages | length' <<<"$history")" -gt "$seen" ] && [ "$(jq -r .active_task_id <<<"$history")" = null ]; then
      echo "助手> $(jq -r '.messages[-1].content' <<<"$history")"
      break
    fi
    sleep 1
  done
  exit 0
fi

echo; echo "我的工单："
curl -sf "$BASE/tickets" -H "$AUTH" | jq -r '.items[] | "  \(.id)  \(.status)  \(.category)  \(.summary)"'
