#!/usr/bin/env python3
"""Prints a trace from the local Tempo as an indented tree: service, span, start offset, duration,
the app's decision attributes, GenAI token usage and span events. SQL spans are folded into a
count on their parent.

    scripts/show-trace.py              the most recent conversation turn
    scripts/show-trace.py <trace_id>   a given trace

Needs the compose stack (Tempo on localhost:23200; override with TEMPO_URL). Standard library only.
"""

import json
import os
import sys
import urllib.parse
import urllib.request

TEMPO = os.environ.get("TEMPO_URL", "http://localhost:23200")
SHOWN_ATTRIBUTES = (
    "app.intent", "app.resolution", "app.category", "app.knowledge_hits", "app.knowledge_error", "app.outcome",
    "app.ticket_id", "app.delivery_attempt", "app.queue_wait_ms", "app.requeue",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "app.time_to_first_token_ms",
)
SQL_VERBS = {";", "select", "insert", "update", "delete", "show", "begin", "commit", "rollback"}


def get(path: str, **params: str) -> dict:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    with urllib.request.urlopen(f"{TEMPO}{path}{query}") as resp:
        return json.load(resp)


def latest_turn() -> str:
    traces = get("/api/search", q='{name="process agent_tasks"}', limit="20")["traces"]
    if not traces:
        sys.exit("no conversation turn in Tempo yet: run scripts/demo.sh first")
    return max(traces, key=lambda t: int(t["startTimeUnixNano"]))["traceID"]


def value(v: dict) -> object:
    return next(iter(v.values()))


def main() -> None:
    trace_id = sys.argv[1] if len(sys.argv) > 1 else latest_turn()
    spans = {}
    for batch in get(f"/api/traces/{trace_id}")["batches"]:
        service = next(value(a["value"]) for a in batch["resource"]["attributes"] if a["key"] == "service.name")
        for scope in batch.get("scopeSpans", []):
            for span in scope["spans"]:
                spans[span["spanId"]] = {**span, "service": service}
    children: dict = {}
    for span in spans.values():
        parent = span.get("parentSpanId")
        children.setdefault(parent if parent in spans else None, []).append(span)
    start = min(int(s["startTimeUnixNano"]) for s in spans.values())

    def is_sql(span: dict) -> bool:
        return span["name"].split()[0].lower() in SQL_VERBS

    def show(span: dict, depth: int) -> None:
        begin = (int(span["startTimeUnixNano"]) - start) / 1e6
        took = (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e6
        attrs = {a["key"]: value(a["value"]) for a in span.get("attributes", [])}
        shown = {k.split(".", 1)[1] if k.startswith("app.") else k: attrs[k] for k in SHOWN_ATTRIBUTES if k in attrs}
        kids = sorted(children.get(span["spanId"], []), key=lambda s: int(s["startTimeUnixNano"]))
        sql = sum(map(is_sql, kids))
        notes = [f"sql×{sql}"] if sql else []
        notes += [f"{k}={v}" for k, v in shown.items()]
        notes += [f"!{e['name']}" for e in span.get("events", [])]
        if span.get("status", {}).get("code") == "STATUS_CODE_ERROR":
            notes.append("ERROR")
        print(f"{begin:8.0f}ms {'  ' * depth}{span['service']}: {span['name']} ({took:.0f}ms) {' '.join(notes)}")
        for kid in kids:
            if not is_sql(kid):
                show(kid, depth + 1)

    print(f"trace {trace_id}")
    for root in sorted(children.get(None, []), key=lambda s: int(s["startTimeUnixNano"])):
        show(root, 0)
    print(f"{len(spans)} spans across {', '.join(sorted({s['service'] for s in spans.values()}))}")


if __name__ == "__main__":
    main()
