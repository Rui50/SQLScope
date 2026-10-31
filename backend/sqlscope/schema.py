""" read a loaded schema """

from dataclasses import dataclass, field

import psycopg

@dataclass
class Column:
    name : str
    type : str # type in postgres, e.g. "bigint" or "character varying(255)"
    nullable : bool
    default : str | None
    identity : str | None                   # "always" or "by default" for GENERATED ... AS IDENTITY
    generated : bool                        # generated ALWAYS AS (...) STORED, cant be inserted into
    enum_values : list[str] | None = None
    checks: list[str] = field(default_factory=list) #CHECKS on this column done


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


@dataclass
class Index:
    name: str
    columns: list[str] # key columns or expressions, in order
    unique: bool
    primary: bool
    definition: str    # the full CREATE INDEX statement


@dataclass
class Table:
    name: str
    columns: dict[str, Column]
    primary_key: list[str] = field(default_factory=list)
    unique: list[list[str]] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)  # CHECK of all columns
    indexes: list[Index] = field(default_factory=list)

@dataclass
class ColumnStats:
    null_frac: float           # share of rows that are NULL
    distinct: float            # estimated number of different values
    common_values: list[str]   # most common values, most frequent first
    common_freqs: list[float]  # share of rows for each of those


# All four queries read Postgres's system catalog (its own description of what exists)
# and only look at ordinary tables in the public schema:
#   relnamespace = 'public'::regnamespace  -> in the public schema
#   relkind = 'r'                          -> a regular table, not a view/index/sequence

# Table names.
# One row per table: (table_name)
TABLES = """
SELECT relname                                   -- table name
FROM pg_class                                    -- every table, index, view, sequence...
WHERE relnamespace = 'public'::regnamespace
  AND relkind = 'r'
ORDER BY relname
"""

# Columns of every table.
# One row per column:
#   (table, column, type, nullable, default, identity, generated, enum_values)
COLUMNS = """
SELECT
    c.relname,                                   -- table name
    a.attname,                                   -- column name
    format_type(a.atttypid, a.atttypmod),        -- readable type, e.g. 'character varying(32)'
    NOT a.attnotnull,                            -- true if NULL is allowed
    pg_get_expr(d.adbin, d.adrelid),             -- default as text, e.g. 'now()'; NULL if none

    -- identity column? Postgres stores 'a' / 'd' / '', we turn it into words
    CASE a.attidentity WHEN 'a' THEN 'always' WHEN 'd' THEN 'by default' END,

    -- generated (computed) column? Postgres stores '' for normal columns
    a.attgenerated <> '',

    -- if the type is an enum ('e'), list its values in their defined order
    CASE WHEN t.typtype = 'e' THEN
        ARRAY(SELECT enumlabel::text FROM pg_enum WHERE enumtypid = t.oid ORDER BY enumsortorder)
    END

FROM pg_attribute a                              -- one row per column of every table
JOIN pg_class c ON c.oid = a.attrelid            -- the table the column belongs to
JOIN pg_type t ON t.oid = a.atttypid             -- the column's type
LEFT JOIN pg_attrdef d                           -- the column's default (most have none, hence LEFT)
    ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind = 'r'
  AND a.attnum > 0                               -- skip hidden system columns (ctid, xmin, ...)
  AND NOT a.attisdropped                         -- skip columns that were deleted
ORDER BY c.relname, a.attnum                     -- columns in the order they were defined
"""

# Primary keys, unique constraints, foreign keys and checks.
# One row per constraint:
#   (table, kind, columns, ref_table, ref_columns, check_expression)
# kind is 'p' primary key, 'u' unique, 'f' foreign key, 'c' check.
# ref_table / ref_columns are only filled for foreign keys, check_expression only for checks.
CONSTRAINTS = """
SELECT
    c.relname,                                   -- table name
    con.contype,                                 -- 'p', 'u', 'f' or 'c'

    -- the constraint's columns. Postgres stores them as column NUMBERS (conkey = {2,3}),
    -- so turn each number back into a name, keeping their order:
    --   unnest            {2,3} -> rows 2, 3
    --   WITH ORDINALITY   adds each row's position, so the order survives
    --   JOIN pg_attribute looks up the column name for each number
    --   ARRAY(...)        packs the names back into a list
    ARRAY(SELECT a.attname::text
          FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
          ORDER BY k.ord),

    ref.relname,                                 -- foreign keys: the table it points to

    -- foreign keys: the columns it points to (same trick, on the referenced table)
    ARRAY(SELECT a.attname::text
          FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum
          ORDER BY k.ord),

    pg_get_expr(con.conbin, con.conrelid)        -- checks: the condition as text

FROM pg_constraint con                           -- one row per constraint
JOIN pg_class c ON c.oid = con.conrelid          -- the table it's on
LEFT JOIN pg_class ref ON ref.oid = con.confrelid  -- the referenced table (only for foreign keys)
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind = 'r'
  AND con.contype IN ('p', 'u', 'f', 'c')
ORDER BY c.relname, con.conname
"""

# Every index, including the ones Postgres created for primary keys and unique constraints.
# One row per index:
#   (table, index_name, is_unique, is_primary, columns, create_statement)
INDEXES = """
SELECT
    c.relname,                                   -- table name
    ic.relname,                                  -- index name
    i.indisunique,                               -- unique index?
    i.indisprimary,                              -- primary key index?

    -- the index's key columns in order: count k = 1..n and ask for the k-th column
    -- (gives an expression like lower(email) for expression indexes)
    ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
          FROM generate_series(1, i.indnkeyatts) AS k
          ORDER BY k),

    pg_get_indexdef(i.indexrelid)                -- the full CREATE INDEX statement

FROM pg_index i                                  -- one row per index
JOIN pg_class c ON c.oid = i.indrelid            -- the table it indexes
JOIN pg_class ic ON ic.oid = i.indexrelid        -- the index itself (where its name lives)
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind = 'r'
ORDER BY c.relname, ic.relname
"""

def read_schema(conn: psycopg.Connection) -> dict[str, Table]:
    tables = {name: Table(name=name, columns={}) for (name,) in conn.execute(TABLES)}

    for table, name, type_, nullable, default, identity, generated, enum_values in conn.execute(COLUMNS):
        tables[table].columns[name] = Column(name, type_, nullable, default, identity, generated, enum_values)

    for table, kind, columns, ref_table, ref_columns, check in conn.execute(CONSTRAINTS):
        t = tables[table]
        if kind == "p":
            t.primary_key = columns
        elif kind == "u":
            t.unique.append(columns)
        elif kind == "f":
            t.foreign_keys.append(ForeignKey(columns, ref_table, ref_columns))
        elif len(columns) == 1:
            t.columns[columns[0]].checks.append(check)
        else:
            t.checks.append(check)

    for table, name, unique, primary, columns, definition in conn.execute(INDEXES):
        tables[table].indexes.append(Index(name, columns, unique, primary, definition))

    return tables

# What the planner believes about each column's data, collected by ANALYZE.
# One row per column:
#   (table, column, null_frac, distinct, common_values, common_freqs)
STATS = """
SELECT
    s.tablename,
    s.attname,
    s.null_frac,                                 -- share of rows that are NULL

    -- n_distinct is negative when it's a share of the rows (-1 = every row different),
    -- so turn it into a count with the table's row count
    CASE WHEN s.n_distinct < 0 THEN -s.n_distinct * c.reltuples ELSE s.n_distinct END,

    s.most_common_vals::text::text[],            -- stored as "anyarray", so go through text
    s.most_common_freqs                          -- share of rows for each common value

FROM pg_stats s                                  -- one row per analyzed column
JOIN pg_class c                                  -- the table, for its row count
    ON c.relname = s.tablename AND c.relnamespace = 'public'::regnamespace
WHERE s.schemaname = 'public'
"""

# the planner's statistics for every column, keyed by (table, column)
def read_stats(conn: psycopg.Connection) -> dict[tuple[str, str], ColumnStats]:
    stats = {}
    for table, column, null_frac, distinct, values, freqs in conn.execute(STATS):
        stats[(table, column)] = ColumnStats(null_frac, distinct, values or [], freqs or [])
    return stats
