""" workload: the queries a typical app would run against the schema """

from dataclasses import dataclass

from psycopg import sql

from sqlscope.schema import Table
from sqlscope.seed import insert_sql, read_checks


@dataclass
class Query:
    name: str
    sql: sql.Composable
    param: tuple[str, str] | None = None  # (table, column)


# typical app queries for every table: lookups, children of a parent, filters, recent rows, writes
def build_workload(tables: dict[str, Table], counts: dict[str, int]) -> list[Query]:
    queries = []
    for table in tables.values():
        name = sql.Identifier(table.name)
        pk = table.primary_key[0] if len(table.primary_key) == 1 else None
        has_created_at = "created_at" in table.columns

        # lookup by primary key: already indexed probably, but a common query
        if pk:
            queries.append(Query(
                f"{table.name} where {pk} = ?",
                sql.SQL("SELECT * FROM {} WHERE {} = %s").format(name, sql.Identifier(pk)),
                (table.name, pk),
            ))

        # children of one parent (a user latest orders): foreign keys get no index by default
        for fk in table.foreign_keys:
            if len(fk.columns) != 1:
                continue
            col = fk.columns[0]
            latest = sql.SQL(" ORDER BY created_at DESC LIMIT 20" if has_created_at else "")
            queries.append(Query(
                f"{table.name} where {col} = ?" + (" (latest 20)" if has_created_at else ""),
                sql.SQL("SELECT * FROM {} WHERE {} = %s{}").format(name, sql.Identifier(col), latest),
                (table.name, col),
            ))

        # the most common and the rarest allowed value: same query, but whether an index
        # helps depends on how many rows match. Plus changing a row to the rare value.
        for col in table.columns.values():
            values, _, _ = read_checks(col, col.type)
            values = col.enum_values or values
            if not values:
                continue
            for value in (values[0], values[-1]):
                queries.append(Query(
                    f"{table.name} where {col.name} = '{value}'",
                    sql.SQL("SELECT * FROM {} WHERE {} = {}").format(name, sql.Identifier(col.name), value),
                ))
            if pk:
                queries.append(Query(
                    f"update {table.name} set {col.name} = '{values[-1]}'",
                    sql.SQL("UPDATE {} SET {} = {} WHERE {} = %s").format(
                        name, sql.Identifier(col.name), values[-1], sql.Identifier(pk)
                    ),
                    (table.name, pk),
                ))

        # rows created in the last day
        if has_created_at:
            queries.append(Query(
                f"{table.name} created in the last day",
                sql.SQL("SELECT * FROM {} WHERE created_at > now() - interval '1 day'").format(name),
            ))

        # one new row, made the same way the seed made the others
        new_row = counts[table.name] + 1
        queries.append(Query(f"insert into {table.name}", insert_sql(table, counts, new_row, new_row)))

    return queries
