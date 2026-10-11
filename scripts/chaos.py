#!/usr/bin/env python3
"""Fault drills for agent-runtime running as several replicas under compose.

Each drill starts N conversations at once (two turns each: "VPN 连不上", then "还是连不上", which
ends in a ticket), injects one fault while they run, waits for every turn to finish, then checks
the databases, Redis and Tempo:

    rolling    restart the replicas one by one (SIGTERM, like a rolling deploy)
    kill       SIGKILL the replica holding the most tasks (a crash)
    scale-in   scale from 3 replicas to 1 (like KEDA scaling in)

Invariants: every turn got exactly one assistant reply, none is stuck or failed, at most one
ticket and one create_ticket call per conversation, no new dead letters. Also reported: turn
latency, deliveries beyond the first, runs restarted from a checkpoint ("resumed"), app-service
requeues.

One more drill injects load instead of a fault:

    noisy      one tenant (demo) sends N messages within 2 s while another (northwind) sends a few

It checks that the quiet tenant's tasks do not queue behind the noisy tenant's (fair scheduling),
and reports each tenant's queue wait and turn latency.

    scripts/chaos.py rolling [-n 20]      one drill
    scripts/chaos.py all                  the three fault drills
    scripts/chaos.py noisy [-n 80]        about 2 model calls per message

Needs the stack started with the drill's overrides (a higher per-employee message limit):

    docker compose -f docker-compose.yml -f scripts/chaos.compose.yml up -d --scale agent-runtime=3

Talks to the real model: a drill of 20 conversations is about 100 model calls. Standard library only.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "scripts/chaos.compose.yml"]
BASE = os.environ.get("BASE", "http://localhost:8001/v1")
TEMPO = os.environ.get("TEMPO_URL", "http://localhost:23200")
TENANT, EMAIL, PASSWORD = "demo", "demo.user@example.com", "demo-password"
MESSAGES = ["VPN 连不上", "按你说的试了，还是连不上"]
# The noisy drill's other tenant: a few messages, one every QUIET_EVERY_S, while the flood drains.
QUIET_TENANT, QUIET_EMAIL, QUIET_MESSAGE = "northwind", "demo.user@northwind.example", "年假怎么申请"
FLOOD_WITHIN_S = 2  # not all in one instant: that many connections at once stall Docker Desktop's port proxy
QUIET_CONVERSATIONS, QUIET_AFTER_S, QUIET_EVERY_S = 8, 3, 1.5
QUIET_WAIT_LIMIT_S = 2  # queue wait p95 the quiet tenant must stay under
REPLICAS = 3
# Turns take a few seconds, so conversations start staggered over the fault's whole window (a
# rolling restart of 3 replicas takes ~15 s) to keep turns in flight whenever a replica goes down.
STAGGER_S = 0.75
FAULT_AFTER_S = 2
TURN_TIMEOUT_S = 400  # a crashed task is reclaimed after 90 s idle, and may need that twice
SCENARIOS = ("rolling", "kill", "scale-in")


def sh(*args: str, check: bool = True) -> str:
    return subprocess.run(args, cwd=ROOT, check=check, capture_output=True, text=True).stdout.strip()


def compose(*args: str) -> str:
    return sh(*COMPOSE, *args)


def psql(db: str, sql: str) -> list[list[str]]:
    out = compose("exec", "-T", "postgres", "psql", "-U", "work_order", "-d", db, "-AtF", "\t", "-c", sql)
    return [line.split("\t") for line in out.splitlines() if line]


def redis(*args: str) -> str:
    return compose("exec", "-T", "redis", "redis-cli", "--raw", *args)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# --- replicas -----------------------------------------------------------------------------------

def replicas() -> list[str]:
    """Running agent-runtime containers (ids)."""
    return compose("ps", "-q", "--status", "running", "agent-runtime").split()


def hostname(container: str) -> str:
    """Its consumer name in the agent_tasks group (Settings.consumer_name is the hostname)."""
    return sh("docker", "inspect", "-f", "{{.Config.Hostname}}", container)


def scale(n: int) -> None:
    compose("up", "-d", "--no-recreate", "--scale", f"agent-runtime={n}", "agent-runtime")
    deadline = time.monotonic() + 60
    while len(replicas()) != n:
        if time.monotonic() > deadline:
            sys.exit(f"agent-runtime did not reach {n} replicas")
        time.sleep(1)


def pending_by_consumer() -> dict[str, int]:
    """Over every tenant's stream. XPENDING summary: count, min id, max id, then consumer/count pairs."""
    held: dict[str, int] = {}
    for tenant in redis("ZRANGE", "agent_sched:tenants", "0", "-1").splitlines():
        pairs = redis("XPENDING", f"agent_tasks:t:{tenant}", "agent_runtime").splitlines()[3:]
        for i in range(0, len(pairs) - 1, 2):
            held[pairs[i]] = held.get(pairs[i], 0) + int(pairs[i + 1])
    return held


# --- faults -------------------------------------------------------------------------------------

def rolling() -> None:
    for container in replicas():
        log(f"restart {container[:12]} (SIGTERM)")
        started = time.monotonic()
        sh("docker", "restart", container)  # waits for the container's stop timeout at most
        log(f"  back after {time.monotonic() - started:.1f}s")
        time.sleep(3)


def kill() -> None:
    pending = pending_by_consumer()
    by_host = {hostname(c): c for c in replicas()}
    busiest = max(by_host, key=lambda h: pending.get(h, 0))
    log(f"SIGKILL {busiest} holding {pending.get(busiest, 0)} tasks (pending per consumer: {pending})")
    sh("docker", "kill", "-s", "KILL", by_host[busiest])


def scale_in() -> None:
    log(f"scale {REPLICAS} -> 1")
    started = time.monotonic()
    scale(1)
    log(f"  done after {time.monotonic() - started:.1f}s")


FAULTS = {"rolling": rolling, "kill": kill, "scale-in": scale_in}


# --- the employee's side ------------------------------------------------------------------------

def call(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    headers = {"content-type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def login(tenant: str = TENANT, email: str = EMAIL) -> str:
    return call("POST", "/auth/login", body={"tenant": tenant, "email": email, "password": PASSWORD})["access_token"]


def converse(token: str, conversation: str, errors: list[str], delay: float, messages: list[str] = MESSAGES,
             poll_s: float = 0.5) -> None:
    """Sends each message once the previous one has its reply (the assistant message carrying its
    task_id; the turn closes a moment before that message is readable)."""
    time.sleep(delay)
    for content in messages:
        task = call("POST", f"/conversations/{conversation}/messages", token, {"content": content})["task_id"]
        deadline, closed_since = time.monotonic() + TURN_TIMEOUT_S, None
        while True:
            history = call("GET", f"/conversations/{conversation}/messages", token)
            if any(m["role"] == "assistant" and m["task_id"] == task for m in history["messages"]):
                break
            if history["active_task_id"] is None:
                closed_since = closed_since or time.monotonic()
                if time.monotonic() - closed_since > 10:  # closed without a reply: failed
                    errors.append(f"{conversation}: turn '{content}' closed without a reply")
                    return
            if time.monotonic() > deadline:
                errors.append(f"{conversation}: turn '{content}' still running after {TURN_TIMEOUT_S}s")
                return
            time.sleep(poll_s)


# --- checks -------------------------------------------------------------------------------------

def ids(values: list[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def tempo_deliveries(conversations: list[str], start: float, end: float) -> list[dict]:
    """Every 'process agent_tasks' span of these conversations: one per delivery. A SIGKILLed
    delivery never exports its span; the next attempt's app.delivery_attempt still shows it."""
    query = ('{name="process agent_tasks"} | select(span.app.conversation_id, span.app.task_id, '
             "span.app.delivery_attempt, span.app.result, span.app.queue_wait_ms)")
    params = {"q": query, "start": str(int(start)), "end": str(int(end) + 60), "limit": "2000", "spss": "50"}
    with urllib.request.urlopen(f"{TEMPO}/api/search?{urllib.parse.urlencode(params)}") as resp:
        traces = json.load(resp).get("traces", [])
    wanted, found = set(conversations), []
    for trace in traces:
        for span_set in trace.get("spanSets", []):
            for span in span_set.get("spans", []):
                attrs = {a["key"]: next(iter(a["value"].values())) for a in span.get("attributes", [])}
                if attrs.get("app.conversation_id") in wanted:
                    found.append(attrs)
    return found


def report(name: str, conversations: list[str], errors: list[str], dead_before: int, start: float) -> bool:
    turns = psql("app_service", f"""
        SELECT t.id, t.status, t.enqueue_count,
               extract(epoch FROM t.completed_at - t.created_at),
               (SELECT count(*) FROM messages m WHERE m.task_id = t.id AND m.role = 'assistant')
        FROM turns t WHERE t.conversation_id IN ({ids(conversations)})""")
    tickets = psql("ticket_service", f"""
        SELECT conversation_id, count(*) FROM tickets WHERE conversation_id IN ({ids(conversations)})
        GROUP BY 1 HAVING count(*) > 1""")
    ticket_calls = psql("tool_gateway", f"""
        SELECT session_id, count(*) FROM tool_calls
        WHERE tool_name = 'create_ticket' AND status = 'success' AND session_id IN ({ids(conversations)})
        GROUP BY 1 HAVING count(*) > 1""")
    dead = int(redis("XLEN", "agent_tasks:dead")) - dead_before
    resumed = sum(
        int(redis("EVAL", "local n = 0 for _, e in ipairs(redis.call('XRANGE', KEYS[1], '-', '+')) do "
                          "local f = e[2] for i = 1, #f, 2 do if f[i] == 'type' and f[i + 1] == 'resumed' "
                          "then n = n + 1 end end end return n", "1", f"conversation:{c}:events"))
        for c in conversations
    )
    time.sleep(25)  # let the last spans reach Tempo's search
    deliveries = tempo_deliveries(conversations, start, time.time())
    max_attempt: dict[str, int] = {}
    for d in deliveries:
        task = d.get("app.task_id", "")
        max_attempt[task] = max(max_attempt.get(task, 0), int(d.get("app.delivery_attempt", 1)))
    results: dict[str, int] = {}
    for d in deliveries:
        results[d.get("app.result", "?")] = results.get(d.get("app.result", "?"), 0) + 1

    done = [t for t in turns if t[1] == "done"]
    latencies = sorted(float(t[3]) for t in done if t[3])
    wrong_replies = [t[0] for t in turns if t[4] != "1"]
    checks = {
        "每轮恰好一条回复": not wrong_replies,
        "没有失败或卡住的轮次": len(done) == len(turns) == len(conversations) * len(MESSAGES) and not errors,
        "每会话最多一张工单": not tickets,
        "create_ticket 没有重复执行": not ticket_calls,
        "没有新死信": dead == 0,
    }
    print()
    print(f"== {name}: {len(conversations)} 个会话，{len(turns)} 轮 ==")
    for label, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {label}")
    if wrong_replies:
        print(f"    回复数不对的轮次: {wrong_replies}")
    for e in errors:
        print(f"    {e}")
    if latencies:
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        print(f"  轮次耗时 p50 {statistics.median(latencies):.1f}s  p95 {p95:.1f}s  最大 {latencies[-1]:.1f}s")
    print(f"  重投的轮次（投递 >1 次）: {sum(1 for a in max_attempt.values() if a > 1)}"
          f"   从 checkpoint 续跑（resumed）: {resumed}"
          f"   app-service 补偿重投: {sum(1 for t in turns if int(t[2]) > 1)}   新死信: {dead}")
    print(f"  各次投递的结果: {dict(sorted(results.items()))}")
    return all(checks.values())


def drill(name: str, n: int) -> bool:
    scale(REPLICAS)
    time.sleep(3)
    token = login()
    conversations = [call("POST", "/conversations", token)["id"] for _ in range(n)]
    dead_before = int(redis("XLEN", "agent_tasks:dead"))
    errors: list[str] = []
    start = time.time()
    log(f"{name}: {n} conversations on {len(replicas())} replicas")
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(converse, token, c, errors, i * STAGGER_S) for i, c in enumerate(conversations)]
        time.sleep(FAULT_AFTER_S)
        fault = threading.Thread(target=FAULTS[name])
        fault.start()
        for f in futures:
            f.result()
        fault.join()
    log(f"{name}: all turns finished after {time.time() - start:.0f}s")
    ok = report(name, conversations, errors, dead_before, start)
    scale(REPLICAS)
    return ok


# --- noisy neighbour ----------------------------------------------------------------------------

def spread(values: list[float]) -> str:
    if not values:
        return "无数据"
    values = sorted(values)
    p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
    return f"p50 {statistics.median(values):.1f}s  p95 {p95:.1f}s  最大 {values[-1]:.1f}s"


def noisy(n: int) -> bool:
    """n conversations of one tenant send a message each within FLOOD_WITHIN_S; a few of another
    tenant's follow, one at a time, while that backlog drains. Without fair scheduling they would
    wait behind it."""
    scale(REPLICAS)
    time.sleep(3)
    loud, quiet = login(), login(QUIET_TENANT, QUIET_EMAIL)
    flood = [call("POST", "/conversations", loud)["id"] for _ in range(n)]
    few = [call("POST", "/conversations", quiet)["id"] for _ in range(QUIET_CONVERSATIONS)]
    dead_before = int(redis("XLEN", "agent_tasks:dead"))
    errors: list[str] = []
    start = time.time()
    log(f"noisy: {n} messages from {TENANT} within {FLOOD_WITHIN_S}s, then {len(few)} from {QUIET_TENANT}, "
        f"on {len(replicas())} replicas")
    with ThreadPoolExecutor(max_workers=n + len(few)) as pool:
        # Polled slowly: the flood's latency is read from the database, and 80 clients polling
        # twice a second would be a load test of app-service rather than of the queue.
        futures = [pool.submit(converse, loud, c, errors, i * FLOOD_WITHIN_S / n, MESSAGES[:1], 3)
                   for i, c in enumerate(flood)]
        futures += [pool.submit(converse, quiet, c, errors, QUIET_AFTER_S + i * QUIET_EVERY_S, [QUIET_MESSAGE])
                    for i, c in enumerate(few)]
        for f in futures:
            f.result()
    log(f"noisy: all turns finished after {time.time() - start:.0f}s")

    time.sleep(25)  # let the last spans reach Tempo's search
    first_deliveries = [d for d in tempo_deliveries(flood + few, start, time.time())
                        if int(d.get("app.delivery_attempt", 1)) == 1 and "app.queue_wait_ms" in d]
    stats = {}
    for tenant, conversations in ((TENANT, flood), (QUIET_TENANT, few)):
        turns = psql("app_service", f"""
            SELECT t.status, extract(epoch FROM t.completed_at - t.created_at),
                   (SELECT count(*) FROM messages m WHERE m.task_id = t.id AND m.role = 'assistant')
            FROM turns t WHERE t.conversation_id IN ({ids(conversations)})""")
        wanted = set(conversations)
        stats[tenant] = {
            "turns": turns,
            "expected": len(conversations),
            "latency": [float(t[1]) for t in turns if t[0] == "done" and t[1]],
            "wait": [int(d["app.queue_wait_ms"]) / 1000 for d in first_deliveries if d["app.conversation_id"] in wanted],
        }
    every_turn = [t for s in stats.values() for t in s["turns"]]
    quiet_waits = sorted(stats[QUIET_TENANT]["wait"])
    quiet_p95 = quiet_waits[min(len(quiet_waits) - 1, int(len(quiet_waits) * 0.95))] if quiet_waits else None
    checks = {
        "每轮恰好一条回复": all(t[2] == "1" for t in every_turn),
        "没有失败或卡住的轮次": not errors and all(
            len(s["turns"]) == s["expected"] and all(t[0] == "done" for t in s["turns"]) for s in stats.values()),
        "没有新死信": int(redis("XLEN", "agent_tasks:dead")) == dead_before,
        f"安静租户的排队 p95 低于 {QUIET_WAIT_LIMIT_S}s": quiet_p95 is not None and quiet_p95 < QUIET_WAIT_LIMIT_S,
    }
    print()
    print(f"== noisy: {TENANT} {n} 条在 {FLOOD_WITHIN_S} 秒内发出，{QUIET_TENANT} {len(few)} 条陆续发出 ==")
    for label, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {label}")
    for e in errors:
        print(f"    {e}")
    for tenant, s in stats.items():
        print(f"  {tenant:<10} 排队 {spread(s['wait'])}   轮次耗时 {spread(s['latency'])}")
    return all(checks.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", choices=[*SCENARIOS, "all", "noisy"])
    parser.add_argument("-n", "--conversations", type=int, help="default: 20, or 80 for noisy")
    args = parser.parse_args()
    try:
        if args.scenario == "noisy":
            results = {"noisy": noisy(args.conversations or 80)}
        else:
            results = {s: drill(s, args.conversations or 20)
                       for s in (SCENARIOS if args.scenario == "all" else [args.scenario])}
    except urllib.error.HTTPError as exc:
        sys.exit(f"{exc.code} {exc.url}: {exc.read().decode()}")
    print()
    print("  ".join(f"{s}: {'通过' if ok else '未通过'}" for s, ok in results.items()))
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
