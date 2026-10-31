""" seed: fill a sandbox with fake rows, generated inside postgres with generate_series """

import re
import time
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

import psycopg
from psycopg import sql

from sqlscope.fake_data import NAME_VALUES, NUMBER_RANGES, TYPE_VALUES
from sqlscope.schema import Column, Table

DEFAULT_ROWS = 10_000

INTEGER_TYPES = {"smallint", "integer", "bigint"}
NUMBER_TYPES = INTEGER_TYPES | {"numeric", "real", "double precision"}
TEXT_TYPES = {"text", "character varying", "character"}


class SeedError(Exception):
    """A table or column we can't fill yet; the message says why."""


@dataclass
class TableFill:
    table: str
    requested: int
    inserted: int  # lower than requested when random rows collided on a unique key
    seconds: float


# fill every table, parents first, then refresh the planner's statistics
def seed_data(
    conn: psycopg.Connection,
    tables: dict[str, Table],
    base_rows: int = DEFAULT_ROWS,
    table_rows: dict[str, int] | None = None,
) -> list[TableFill]:
    table_rows = table_rows or {}
    unknown = set(table_rows) - set(tables)
    if unknown:
        raise SeedError(f"no table named {', '.join(sorted(unknown))}")
    order = fill_order(tables)

    # each foreign key level down gets 3x the rows: users 10k -> orders 30k -> order_items 90k
    depth: dict[str, int] = {}
    counts: dict[str, int] = {}
    for name in order:
        depth[name] = 0
        for fk in tables[name].foreign_keys:
            if fk.ref_table == name:
                continue  # pointing at itself doesn't make it a level deeper
            depth[name] = max(depth[name], depth[fk.ref_table] + 1)
        counts[name] = table_rows.get(name, base_rows * 3 ** depth[name])

    fills = []
    for name in order:
        start = time.perf_counter()
        inserted = fill_table(conn, tables[name], counts)
        fills.append(TableFill(name, counts[name], inserted, time.perf_counter() - start))

    conn.execute(sql.SQL("ANALYZE {}").format(sql.SQL(", ").join(map(sql.Identifier, tables))))
    return fills


# parents before children, so foreign keys always point at rows that exist
def fill_order(tables: dict[str, Table]) -> list[str]:
    # each table -> the tables it points at (pointing at itself doesn't count)
    graph = {
        name: {fk.ref_table for fk in table.foreign_keys if fk.ref_table != name}
        for name, table in tables.items()
    }
    try:
        return list(TopologicalSorter(graph).static_order())
    except CycleError as e:
        raise SeedError(f"foreign keys form a cycle: {' -> '.join(e.args[1])}") from e


# insert all of one table's rows with a single INSERT ... SELECT, returns how many went in
def fill_table(conn: psycopg.Connection, table: Table, counts: dict[str, int]) -> int:
    inserted = conn.execute(insert_sql(table, counts, 1, counts[table.name])).rowcount

    # we set the ids ourselves, so move each id counter past them,
    # otherwise the next normal INSERT gets id 1 again and collides
    for col in table.columns.values():
        if col.identity or (col.default or "").startswith("nextval("):
            # pg_get_serial_sequence reads the table name as SQL, so it needs the quoted form
            conn.execute(
                sql.SQL("SELECT setval(pg_get_serial_sequence({}, {}), max({})) FROM {}").format(
                    sql.Identifier(table.name).as_string(conn),
                    col.name,
                    sql.Identifier(col.name),
                    sql.Identifier(table.name),
                )
            )
    return inserted


# INSERT ... SELECT that creates rows number first..last of a table
def insert_sql(table: Table, counts: dict[str, int], first: int, last: int) -> sql.Composable:
    columns = [c for c in table.columns.values() if not c.generated]  # postgres computes those
    overriding = any(c.identity == "always" for c in columns)

    # builds one INSERT that creates all rows at once, e.g. for orders:
    #   INSERT INTO orders (id, user_id, ...)
    #   SELECT g, <random user>, ... FROM generate_series(first, last) AS g
    # generate_series makes the row numbers (g), the SELECT turns each one into a row.
    # OVERRIDING SYSTEM VALUE: allows setting ids on GENERATED ALWAYS columns
    # ON CONFLICT DO NOTHING: a random duplicate (same order + product twice) is skipped
    #                         instead of cancelling the whole insert
    return sql.SQL(
        "INSERT INTO {table} ({columns}) {overriding}"
        "SELECT {values} FROM generate_series({first}, {last}) AS g "
        "ON CONFLICT DO NOTHING"
    ).format(
        table=sql.Identifier(table.name),
        columns=sql.SQL(", ").join(sql.Identifier(c.name) for c in columns),
        overriding=sql.SQL("OVERRIDING SYSTEM VALUE " if overriding else ""),
        values=sql.SQL(", ").join(value_sql(table, c, counts) for c in columns),
        first=first,
        last=last,
    )


# the SQL expression that produces this column's value for row g
def value_sql(table: Table, col: Column, counts: dict[str, int]) -> sql.Composable:
    base = re.sub(r"\(.*?\)", "", col.type)  # 'character varying(32)' -> 'character varying'
    fk = next((fk for fk in table.foreign_keys if fk.columns == [col.name]), None)
    values, low, high = read_checks(col, base)

    # foreign key to its own table (categories.parent_id)
    if fk and fk.ref_table == table.name:
        expr = sql.SQL("NULL" if col.nullable else "g")  # no parent, or itself if NULL isn't allowed

    # foreign key to another table (orders.user_id)
    elif fk:
        # low ids far more often: the top 1% of parents get ~21% of the children
        expr = sql.SQL("1 + floor(random() ^ 3 * {})").format(counts[fk.ref_table])

    # integer id, primary key or unique (users.id)
    elif base in INTEGER_TYPES and (table.primary_key == [col.name] or [col.name] in table.unique):
        expr = sql.SQL("g")  # ids 1..N, so children know exactly which ids exist

    # fixed list of allowed values, from an enum or CHECK IN (...) (orders.status, users.plan)
    elif col.enum_values or values:
        # the first values far more often (delivered ~60%, cancelled ~7%)
        options = col.enum_values or values
        expr = sql.SQL("(ARRAY[{}])[1 + floor(random() ^ 3 * {})::int]").format(
            sql.SQL(", ").join(map(sql.Literal, options)), len(options)
        )

    # unique text (users.email, products.sku)
    elif base in TEXT_TYPES and [col.name] in table.unique:
        # never repeats: user1@example.com for emails, 'sku_1', 'sku_2', ... for the rest
        if "email" in col.name:
            expr = sql.SQL("'user' || g || '@example.com'")
        else:
            expr = sql.SQL("{} || g").format(f"{col.name}_")

    # a column name with realistic values in fake_data.py (users.full_name, users.country)
    elif col.name in NAME_VALUES:
        expr = sql.SQL(NAME_VALUES[col.name])

    # any other number, within its CHECK bounds (reviews.rating, products.price)
    elif base in NUMBER_TYPES:
        expr = number_sql(col, base, low, high)

    # any other type with a simple random value in fake_data.py (text, boolean, timestamps...)
    elif base in TYPE_VALUES:
        expr = sql.SQL(TYPE_VALUES[base])

    # unknown type, but empty is allowed
    elif col.nullable:
        expr = sql.SQL("NULL")

    # unknown type that can't be empty: stop with a clear message
    else:
        raise SeedError(f"{table.name}.{col.name}: don't know how to generate {col.type} yet")

    # 10% empty where allowed (foreign keys stay filled, so joins find their parents)
    if col.nullable and not fk:
        expr = sql.SQL("CASE WHEN random() < 0.1 THEN NULL ELSE {} END").format(expr)

    # cast to the column's type: 'pending' -> order_status, long text cut down to varchar(32),
    # 12.3456 rounded to numeric(10,2)
    return sql.SQL("({})::{}").format(expr, sql.SQL(col.type))


# a random number between the CHECK bounds, else the column's usual range, else 0..1000
def number_sql(col: Column, base: str, low: float | None, high: float | None) -> sql.Composable:
    usual_low, usual_high = NUMBER_RANGES.get(col.name, (0, 1000))
    if low is None:
        low = usual_low if high is None or high > usual_low else high - 1000
    if high is None:
        high = usual_high if usual_high > low else low + 1000

    # numeric(10,2) holds at most 99999999.99, so stay below 10^(10-2)
    match = re.fullmatch(r"numeric\((\d+),(\d+)\)", col.type)
    if match:
        high = min(high, 10 ** (int(match[1]) - int(match[2])) - 1)

    if base in INTEGER_TYPES:
        # every whole number from low to high
        return sql.SQL("{} + floor(random() * {})").format(int(low), int(high) - int(low) + 1)
    return sql.SQL("{} + random() * {}").format(low, high - low)


# a column's CHECKs as (allowed values, low, high); postgres hands them back in a fixed form:
#   plan IN ('free', 'pro')  ->  (plan = ANY (ARRAY['free'::text, 'pro'::text]))
#   rating BETWEEN 1 AND 5   ->  ((rating >= 1) AND (rating <= 5))
#   quantity > 0             ->  (quantity > 0)
def read_checks(col: Column, base: str) -> tuple[list[str] | None, float | None, float | None]:
    values, low, high = None, None, None
    step = 1 if base in INTEGER_TYPES else 0.01  # smallest move off a strict > or < bound
    for check in col.checks:
        if "= ANY (ARRAY[" in check:
            # the quoted strings ('' inside one is an escaped quote), or plain numbers
            array = check[check.index("ARRAY["):]
            quoted = [v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", array)]
            values = quoted or re.findall(r"-?\d+(?:\.\d+)?", array)
        for op, number in re.findall(r"(>=|<=|>|<) \(?(-?\d+(?:\.\d+)?)", check):
            if op == ">=":
                low = float(number)
            elif op == ">":
                low = float(number) + step
            elif op == "<=":
                high = float(number)
            else:
                high = float(number) - step
    return values, low, high
