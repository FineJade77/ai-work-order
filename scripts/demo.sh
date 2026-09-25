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
  *) echo "usage: $0 vpn|account|email|printer|leave" >&2; exit 2 ;;
esac

TOKEN="$(curl -sf "$BASE/auth/login" -H 'content-type: application/json' \
  -d "$(jq -n --arg t "$TENANT" --arg e "$EMAIL" --arg p "$PASSWORD" '{tenant: $t, email: $e, password: $p}')" \
  | jq -r .access_token)"
AUTH="Authorization: Bearer $TOKEN"
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

echo; echo "我的工单："
curl -sf "$BASE/tickets" -H "$AUTH" | jq -r '.items[] | "  \(.id)  \(.status)  \(.category)  \(.summary)"'
