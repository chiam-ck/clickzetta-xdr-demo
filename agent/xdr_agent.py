#!/usr/bin/env python3
"""xdr_agent.py — consumer agent over cz-cli (read-only profile 'agent').

Backed by DeepSeek (OpenAI-compatible API). Credentials come from
DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL, read from the environment or .env.clickzetta.

    python3 agent/xdr_agent.py "Which cells had the highest data volume today?"
    python3 agent/xdr_agent.py --dry-run "..."   # print tool calls, execute no SQL

Every tool call is appended to evidence/agent/tool_calls.jsonl.
"""
import argparse, json, os, subprocess, sys, time
from pathlib import Path

from openai import OpenAI

MODEL = "deepseek-v4-flash"
CZ = os.path.expanduser("~/.clickzetta/bin/cz-cli")
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "evidence" / "agent" / "tool_calls.jsonl"
DRY_RUN = False

SYSTEM = """You are a telco NOC analyst for a Singapore mobile operator, answering questions
over an xDR lakehouse (schema xdr_demo) through a READ-ONLY identity.

Objects: ods_xdr_raw (raw xDRs; msisdn is masked for you), dwd_session (deduped sessions,
one row per xdr_id at latest record_version, with event_hour and lateness_h), dynamic tables
ads_hourly_cell_usage (event_hour, cell_id, rat, sessions, uniq_subs, bytes_dl/ul, revenue,
very_late_records), ads_subscriber_daily, ads_late_arrival_audit, and semantic view
sv_network_usage (dimensions usage.event_hour/cell/radio/reliability; metrics usage.sessions/
unique_subscribers/data_gb/revenue/gb_per_session).

Rules: prefer semantic_query for business metrics. The semantic view can ONLY be read through
the semantic_query tool — a plain SELECT FROM sv_network_usage fails; use run_sql for the
tables. Whenever you report per-hour figures you MUST also state each hour's reliability
(PROVISIONAL means late roaming records may still change it; SETTLED otherwise) using the
usage.reliability dimension. Timestamps are UTC+8. Queries without LIMIT are capped at 100
rows. Be economical: plan 1-4 queries, then give your final answer with the numbers — do not
keep re-verifying data you already have."""

TOOLS = [
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "Run one read-only SQL statement on the ClickZetta lakehouse and "
                       "return the result. Writes are rejected by the platform.",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "A single SELECT/SHOW/DESC statement."}},
            "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "semantic_query",
        "description": "Query the governed semantic view sv_network_usage.",
        "parameters": {"type": "object", "properties": {
            "dimensions": {"type": "string", "description":
                           "Comma-separated dimensions, e.g. 'usage.event_hour, usage.reliability'."},
            "metrics": {"type": "string", "description":
                        "Comma-separated metrics, e.g. 'usage.sessions, usage.data_gb'."},
            "where": {"type": "string", "description":
                      "Optional SQL predicate applied outside the semantic_view() call."},
            "order_by": {"type": "string", "description":
                         "Optional ORDER BY over the output columns."},
            "limit": {"type": "integer", "description": "Row limit (default 24)."}},
            "required": ["dimensions", "metrics"]}}},
]


def _env() -> dict:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        for line in (ROOT / ".env.clickzetta").read_text().splitlines():
            if line.startswith(("DEEPSEEK_API_KEY=", "DEEPSEEK_BASE_URL=")):
                k, _, v = line.partition("=")
                os.environ.setdefault(k, v)
    return os.environ


def _log(tool: str, args: dict, result: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": MODEL,
                            "tool": tool, "args": args, "dry_run": DRY_RUN,
                            "result_preview": result[:500]}) + "\n")


def _cz_sql(sql: str) -> str:
    if DRY_RUN:
        return "DRY RUN: not executed"
    sub = subprocess.run([CZ, "--profile", "agent", "sql", sql],
                         capture_output=True, text=True, timeout=120)
    try:
        job_id = json.loads(sub.stdout)["data"]["job_id"]
    except (json.JSONDecodeError, KeyError):
        return f"submit failed: {sub.stdout[:800]} {sub.stderr[:200]}"
    res = subprocess.run([CZ, "--profile", "agent", "job", "result", job_id, "-o", "toon"],
                         capture_output=True, text=True, timeout=300)
    return (res.stdout or res.stderr)[:6000]


def run_sql(sql: str) -> str:
    out = _cz_sql(sql)
    _log("run_sql", {"sql": sql}, out)
    print(f"  [tool] run_sql: {sql[:100].replace(chr(10), ' ')}...", file=sys.stderr)
    return out


def semantic_query(dimensions: str, metrics: str, where: str = "", order_by: str = "",
                   limit: int = 24) -> str:
    sql = (f"SELECT * FROM semantic_view(xdr_demo.sv_network_usage "
           f"DIMENSIONS {dimensions} METRICS {metrics})")
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    sql += f" LIMIT {limit}"
    out = _cz_sql(sql)
    _log("semantic_query", {"dimensions": dimensions, "metrics": metrics, "where": where,
                            "order_by": order_by, "limit": limit, "sql": sql}, out)
    print(f"  [tool] semantic_query: {sql[:100]}...", file=sys.stderr)
    return out


FUNCS = {"run_sql": run_sql, "semantic_query": semantic_query}


def main() -> None:
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--dry-run", action="store_true",
                    help="print tool calls without executing the SQL")
    a = ap.parse_args()
    DRY_RUN = a.dry_run

    env = _env()
    client = OpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env["DEEPSEEK_BASE_URL"])
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": a.question}]
    CAP = 16
    for i in range(CAP):
        last = i == CAP - 1
        resp = client.chat.completions.create(model=MODEL, messages=messages,
                                              tools=None if last else TOOLS,
                                              temperature=0.2)
        msg = resp.choices[0].message
        messages.append(msg)
        if not msg.tool_calls:
            print(msg.content or "")
            return
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments)
            result = FUNCS[call.function.name](**args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        if i == CAP - 3:
            messages.append({"role": "user", "content":
                             "You have enough data. Give your final answer now, "
                             "without any further tool calls."})
    sys.exit("tool-loop cap reached without a final answer")


if __name__ == "__main__":
    main()
