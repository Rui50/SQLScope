""" measure: run each workload query a few times and record its plan, time and pages read """

import statistics
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from sqlscope.workload import Query

RUNS = 5


@dataclass
class Measurement:
    query: Query
    # each different plan seen across the runs -> the sampled values that got it (usually one plan)
    plans: dict[str, list] = field(default_factory=dict)
    ms: float = 0               # median execution time
    pages: int = 0              # median 8 KB pages touched, from memory or disk
    rows: int = 0               # median rows returned
    skipped: str | None = None  # why it didn't run


# sampled values for every query, taken once so the baseline, the screening and the run with
# new indexes all test the same values (query name -> values)
def sample_workload(conn: psycopg.Connection, queries: list[Query]) -> dict[str, list | None]:
    return {query.name: sample_params(conn, query) for query in queries}


# measure every query in the workload with its sampled values
def measure_workload(conn: psycopg.Connection, queries: list[Query], samples: dict[str, list | None]) -> list[Measurement]:
    return [measure_query(conn, query, samples[query.name]) for query in queries]


# run the query once to warm up, then once per sampled value, keep the medians
def measure_query(conn: psycopg.Connection, query: Query, params: list | None) -> Measurement:
    if params is None:
        return Measurement(query, skipped="no values to sample")

    explain = sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {}").format(query.sql)
    # writes the values into the SQL, so the planner sees them
    cursor = psycopg.ClientCursor(conn) 
    # warm-up: loads the pages into memory, not counted
    run_once(conn, cursor, explain, params[0]) 
    results = [run_once(conn, cursor, explain, p) for p in params]

    # which plan each value got: a busy user can get a different plan than an average one
    plans: dict[str, list] = {}
    for result, param in zip(results, params):
        plans.setdefault(plan_summary(result["Plan"]), []).append(param[0] if param else None)

    return Measurement(
        query,
        plans=plans,
        ms=statistics.median(r["Execution Time"] for r in results),
        pages=int(statistics.median(r["Plan"]["Shared Hit Blocks"] + r["Plan"]["Shared Read Blocks"] for r in results)),
        rows=int(statistics.median(r["Plan"]["Actual Rows"] for r in results)),
    )


# RUNS values for the query's %s, taken from random rows so popular values (a busy user) come up
# as often as in a real app. [None] * RUNS when it has no %s, None when there's nothing to sample.
def sample_params(conn: psycopg.Connection, query: Query) -> list | None:
    if not query.param:
        return [None] * RUNS
    table, column = query.param
    samples = conn.execute(
        sql.SQL("SELECT {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY random() LIMIT {n}").format(
            col=sql.Identifier(column), table=sql.Identifier(table), n=RUNS
        )
    ).fetchall()
    if not samples:
        return None
    return [samples[i % len(samples)] for i in range(RUNS)]


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
