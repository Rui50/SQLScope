""" measure: run each workload query a few times and record its plan, time and pages read """

import statistics
from dataclasses import dataclass

import psycopg
from psycopg import sql

from sqlscope.workload import Query

RUNS = 5


@dataclass
class Measurement:
    query: Query
    plans: list[str]            # the different plans seen across the runs, usually just one
    ms: float = 0               # median execution time
    pages: int = 0              # median 8 KB pages touched, from memory or disk
    rows: int = 0               # median rows returned
    skipped: str | None = None  # why it didn't run


# measure every query in the workload
def measure_workload(conn: psycopg.Connection, queries: list[Query]) -> list[Measurement]:
    return [measure_query(conn, query) for query in queries]


# sample values for the query's %s, run it once to warm up, then RUNS times, keep the medians
def measure_query(conn: psycopg.Connection, query: Query) -> Measurement:
    if query.param:
        table, column = query.param
        # random rows, so popular values (a busy user) come up as often as in a real app
        samples = conn.execute(
            sql.SQL("SELECT {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY random() LIMIT {n}").format(
                col=sql.Identifier(column), table=sql.Identifier(table), n=RUNS
            )
        ).fetchall()
        if not samples:
            return Measurement(query, [], skipped="no values to sample")
        params = [samples[i % len(samples)] for i in range(RUNS)]
    else:
        params = [None] * RUNS

    explain = sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {}").format(query.sql)
    # writes the values into the SQL, so the planner sees them
    cursor = psycopg.ClientCursor(conn) 
    # warm-up: loads the pages into memory, not counted
    run_once(conn, cursor, explain, params[0]) 
    results = [run_once(conn, cursor, explain, p) for p in params]

    return Measurement(
        query,
        plans=sorted({plan_summary(r["Plan"]) for r in results}),
        ms=statistics.median(r["Execution Time"] for r in results),
        pages=int(statistics.median(r["Plan"]["Shared Hit Blocks"] + r["Plan"]["Shared Read Blocks"] for r in results)),
        rows=int(statistics.median(r["Plan"]["Actual Rows"] for r in results)),
    )


# run EXPLAIN ANALYZE once inside a transaction that's always rolled back, so writes leave no trace
def run_once(conn: psycopg.Connection, cursor: psycopg.ClientCursor, explain: sql.Composable, params: tuple | None) -> dict:
    with conn.transaction(force_rollback=True):
        return cursor.execute(explain, params).fetchone()[0][0]


# the interesting steps of a plan: "Seq Scan on orders", "Index Scan using orders_pkey", "Insert on orders"
def plan_summary(node: dict) -> str:
    kind = node["Node Type"]
    if kind == "ModifyTable":
        steps = [f"{node['Operation']} on {node['Relation Name']}"]
    elif kind == "Seq Scan":
        steps = [f"Seq Scan on {node['Relation Name']}"]
    elif "Index Name" in node:  # Index Scan, Index Only Scan, Bitmap Index Scan
        steps = [f"{kind} using {node['Index Name']}"]
    else:
        steps = []  # Limit, Sort, Result... the scans below them are what matters here

    for child in node.get("Plans", []):
        child_steps = plan_summary(child)
        if child_steps:
            steps.append(child_steps)
    return " → ".join(steps)
