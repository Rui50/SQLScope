""" recommend: propose indexes for the workload, then test them as hypothetical indexes (hypopg) """

import statistics
from dataclasses import dataclass, field

import psycopg
from pglast import ast, parse_sql
from pglast.enums import A_Expr_Kind, BoolExprType
from psycopg import sql

from sqlscope.measure import Measurement, measure_query, plan_summary
from sqlscope.schema import Table
from sqlscope.workload import Query

RANGE_OPS = {"<", "<=", ">", ">="}
MIN_GAIN = 0.3  # the planner must expect at least 30% lower cost, or an index isn't worth its upkeep
KEEP_GAIN = 5  # an index that makes some query this many times faster is a clear win
MARGINAL_GAIN = 1.5  # below this it isn't worth its upkeep at all


@dataclass
class Candidate:
    table: str
    columns: tuple[str, ...]
    queries: list[Query] = field(default_factory=list)  # the queries it was proposed for


@dataclass
class Estimate:
    query: Query
    cost_before: float  # planner's estimated cost without the index (median over the sampled values)
    cost_after: float   # ...and with the hypothetical index
    used: int           # how many sampled values got a plan that uses the index
    runs: int
    plan_before: str    # the plan it keeps when the index isn't used


@dataclass
class Screening:
    candidate: Candidate
    estimates: list[Estimate]


@dataclass
class BuiltIndex:
    candidate: Candidate
    name: str
    size: int  # bytes on disk


@dataclass
class Forced:
    index: BuiltIndex
    normal: Measurement  # what the planner chose
    forced: Measurement  # the same query with Seq Scans switched off


@dataclass
class Verdict:
    index: BuiltIndex
    verdict: str                     # keep, marginal or drop
    helps: list[tuple[str, float]]   # queries whose plan now uses it, and how many times faster
    hurts: list[tuple[str, float]]   # ...and those that got slower, how many times slower
    writes: list[tuple[str, int, int]]  # inserts/updates on its table: query, pages before, after
    shared_with: int                 # other new indexes on the same table, sharing that write cost


@dataclass
class Redundant:
    name: str        # the existing index
    covered_by: str  # the new index that starts with the same columns


# candidate indexes for each single-table SELECT: its equality columns, then one range or sort column
def find_candidates(queries: list[Query], tables: dict[str, Table]) -> list[Candidate]:
    candidates: dict[tuple[str, tuple[str, ...]], Candidate] = {}
    for query in queries:
        parsed = read_query(query)
        if parsed is None:
            continue
        table, equal, ranged, sort = parsed

        # WHERE user_id = ? ORDER BY created_at  ->  (user_id) and (user_id, created_at)
        extra = (ranged + sort)[:1]
        options = []
        for columns in (tuple(equal), tuple(equal + extra)):
            if columns and columns not in options:
                options.append(columns)

        for columns in options:
            if covered(tables[table], columns):
                continue
            candidates.setdefault((table, columns), Candidate(table, columns)).queries.append(query)
    return list(candidates.values())


# a SELECT's table, its = columns, its < > columns and its ORDER BY columns;
# None when it isn't a plain single-table SELECT (writes and joins aren't handled yet)
def read_query(query: Query) -> tuple[str, list[str], list[str], list[str]] | None:
    text = query.sql.as_string().replace("%s", "$1")  # pglast reads $1 as a parameter
    stmt = parse_sql(text)[0].stmt
    if not isinstance(stmt, ast.SelectStmt) or len(stmt.fromClause or ()) != 1:
        return None
    if not isinstance(stmt.fromClause[0], ast.RangeVar):
        return None

    # WHERE a = 1 AND b > 2  ->  [a = 1, b > 2]
    where = stmt.whereClause
    if isinstance(where, ast.BoolExpr) and where.boolop == BoolExprType.AND_EXPR:
        conditions = where.args
    else:
        conditions = [where] if where is not None else []

    equal, ranged = [], []
    for cond in conditions:
        if isinstance(cond, ast.A_Expr) and cond.kind == A_Expr_Kind.AEXPR_OP and isinstance(cond.lexpr, ast.ColumnRef):
            column = cond.lexpr.fields[-1].sval
            op = cond.name[0].sval
            if op == "=":
                equal.append(column)
            elif op in RANGE_OPS:
                ranged.append(column)

    sort = [s.node.fields[-1].sval for s in stmt.sortClause or () if isinstance(s.node, ast.ColumnRef)]
    return stmt.fromClause[0].relname, equal, ranged, sort


# an existing index already starts with these columns (orders_pkey covers (id))
def covered(table: Table, columns: tuple[str, ...]) -> bool:
    return any(tuple(index.columns[: len(columns)]) == columns for index in table.indexes)


# create each candidate as a hypothetical index and ask the planner (plain EXPLAIN, nothing runs)
# whether it would use it and how much cheaper its queries would get
def screen_candidates(
    conn: psycopg.Connection, candidates: list[Candidate], samples: dict[str, list | None]
) -> list[Screening]:
    cursor = psycopg.ClientCursor(conn)  # writes the values into the SQL, so the planner sees them

    # plans without any hypothetical index, per query (queries with nothing to sample are left out)
    before: dict[str, tuple[list, list[dict]]] = {}
    for candidate in candidates:
        for query in candidate.queries:
            params = samples[query.name]
            if query.name not in before and params is not None:
                before[query.name] = (params, [explain_plan(cursor, query, p) for p in params])

    screenings = []
    for candidate in candidates:
        create = sql.SQL("CREATE INDEX ON {} ({})").format(
            sql.Identifier(candidate.table), sql.SQL(", ").join(map(sql.Identifier, candidate.columns))
        )
        # hypopg names it something like <13542>btree_orders_user_id
        index_name = conn.execute("SELECT indexname FROM hypopg_create_index(%s)", [create.as_string(conn)]).fetchone()[0]

        estimates = []
        for query in candidate.queries:
            if query.name not in before:
                continue
            params, plans = before[query.name]
            after = [explain_plan(cursor, query, p) for p in params]
            estimates.append(Estimate(
                query,
                cost_before=statistics.median(p["Total Cost"] for p in plans),
                cost_after=statistics.median(p["Total Cost"] for p in after),
                used=sum(index_name in plan_summary(p) for p in after),
                runs=len(after),
                plan_before=plan_summary(plans[0]),
            ))

        conn.execute("SELECT hypopg_reset()")  # forget it before trying the next one
        if estimates:
            screenings.append(Screening(candidate, estimates))
    return screenings


# the planner's plan for one run of a query, estimated only: EXPLAIN without ANALYZE runs nothing
def explain_plan(cursor: psycopg.ClientCursor, query: Query, params: tuple | None) -> dict:
    explain = sql.SQL("EXPLAIN (FORMAT JSON) {}").format(query.sql)
    return cursor.execute(explain, params).fetchone()[0][0]["Plan"]


# for each query, the candidate the planner liked best: the lowest estimated cost. When two are within
# 10% of each other, the one that also covers the query's ORDER BY (it keeps working for busy values
# like user 1, which the sampled values can miss), else the one with fewer columns (smaller, cheaper
# to keep up to date). Candidates it never used, or that save less than MIN_GAIN, don't count.
# Returns query name -> (candidate, its estimate)
def choose_indexes(screenings: list[Screening]) -> dict[str, tuple[Candidate, Estimate]]:
    best: dict[str, tuple[Candidate, Estimate]] = {}
    for screening in screenings:
        candidate = screening.candidate
        for estimate in screening.estimates:
            if not estimate.used or estimate.cost_after > (1 - MIN_GAIN) * estimate.cost_before:
                continue
            name = estimate.query.name
            if name not in best:
                best[name] = (candidate, estimate)
                continue

            current_candidate, current_estimate = best[name]
            close = abs(estimate.cost_after - current_estimate.cost_after) <= 0.1 * current_estimate.cost_after
            if close:
                sort = read_query(estimate.query)[3]
                covers_sort = bool(sort) and sort[0] in candidate.columns
                current_covers_sort = bool(sort) and sort[0] in current_candidate.columns
                if covers_sort != current_covers_sort:
                    better = covers_sort
                else:
                    better = len(candidate.columns) < len(current_candidate.columns)
            else:
                better = estimate.cost_after < current_estimate.cost_after
            if better:
                best[name] = (candidate, estimate)
    return best


# create the chosen indexes for real (each once, even when several queries chose it), then
# refresh the statistics; returns each index with its name and size on disk
def build_indexes(conn: psycopg.Connection, best: dict[str, tuple[Candidate, Estimate]]) -> list[BuiltIndex]:
    built = []
    for candidate, _ in best.values():
        if any(b.candidate is candidate for b in built):
            continue
        name = f"{candidate.table}_{'_'.join(candidate.columns)}_idx"  # orders_user_id_created_at_idx
        conn.execute(
            sql.SQL("CREATE INDEX {} ON {} ({})").format(
                sql.Identifier(name),
                sql.Identifier(candidate.table),
                sql.SQL(", ").join(map(sql.Identifier, candidate.columns)),
            )
        )
        size = conn.execute("SELECT pg_relation_size(%s::regclass)", [sql.Identifier(name).as_string(conn)]).fetchone()[0]
        built.append(BuiltIndex(candidate, name, size))

    tables = {b.candidate.table for b in built}
    if tables:
        conn.execute(sql.SQL("ANALYZE {}").format(sql.SQL(", ").join(map(sql.Identifier, sorted(tables)))))
    return built


# queries an index was proposed for where the planner still didn't use it once built: run them again
# with Seq Scans switched off, to see whether the planner was right to skip the index
def force_skipped(
    conn: psycopg.Connection, screenings: list[Screening], built: list[BuiltIndex],
    after: list[Measurement], samples: dict[str, list | None],
) -> list[Forced]:
    after_by_query = {m.query.name: m for m in after}
    forced = []
    for index in built:
        screening = next(s for s in screenings if s.candidate is index.candidate)
        for estimate in screening.estimates:
            normal = after_by_query[estimate.query.name]
            if normal.skipped or any(index.name in plan for plan in normal.plans):
                continue
            query = estimate.query
            forced.append(Forced(index, normal, measure_query(conn, query, samples[query.name], no_seqscan=True)))
    return forced


# judge each built index by every query whose plan now uses it (not just the ones it was chosen
# for) and by the writes on its table: keep, marginal or drop
def judge_indexes(built: list[BuiltIndex], before: list[Measurement], after: list[Measurement]) -> list[Verdict]:
    verdicts = []
    for index in built:
        table = index.candidate.table
        helps, hurts, writes = [], [], []
        for b, a in zip(before, after):
            if b.skipped:
                continue
            if any(index.name in plan for plan in a.plans):
                times = b.ms / max(a.ms, 0.001)
                if times >= 1:
                    helps.append((b.query.name, times))
                else:
                    hurts.append((b.query.name, 1 / times))
            elif next(iter(a.plans)).startswith((f"Insert on {table}", f"Update on {table}")):
                writes.append((b.query.name, b.pages, a.pages))

        best = max((times for _, times in helps), default=1)
        if best >= KEEP_GAIN:
            verdict = "keep"
        elif best >= MARGINAL_GAIN:
            verdict = "marginal"
        else:
            verdict = "drop"
        shared_with = sum(1 for other in built if other.candidate.table == table) - 1
        verdicts.append(Verdict(index, verdict, sorted(helps, key=lambda h: -h[1]), hurts, writes, shared_with))
    return verdicts


# existing indexes a recommended one makes pointless: the new one starts with the same columns, so it
# can do everything the old one did. Primary keys and unique indexes stay, they enforce rules.
def find_redundant(tables: dict[str, Table], verdicts: list[Verdict]) -> list[Redundant]:
    redundant = []
    for v in verdicts:
        if v.verdict == "drop":
            continue  # not recommended, so it can't replace anything
        index = v.index
        columns = index.candidate.columns
        for existing in tables[index.candidate.table].indexes:
            if existing.unique or existing.primary:
                continue
            if len(existing.columns) < len(columns) and tuple(existing.columns) == columns[: len(existing.columns)]:
                redundant.append(Redundant(existing.name, index.name))
    return redundant
