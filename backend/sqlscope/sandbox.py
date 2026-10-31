""" sandbox: fresh database and role for each run, dropped after """

import secrets
from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from sqlscope.config import DATABASE_URL

EXTENSIONS = ["hypopg", "pg_stat_statements"]

@dataclass
class Sandbox:
    name : str # for db and role name
    connection_info : str # connects as the sandbox role not admin

def admin_connection(dbName: str | None = None) -> psycopg.Connection:
    # CREATE/DROP database cant run inside a transaction, so we need autocommit mode
    conninfo = make_conninfo(DATABASE_URL, dbname=dbName) if dbName else DATABASE_URL
    return psycopg.connect(conninfo, autocommit=True)

def create_sandbox() -> Sandbox:
    # name for both database and role
    name = f"sqlscope_sandbox_{secrets.token_hex(4)}"
    # password for new role
    password = secrets.token_urlsafe(16)
    name_sql = sql.Identifier(name)

    try:
        with admin_connection() as admin:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(name_sql, sql.Literal(password)),
            )
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(name_sql, name_sql),
            )
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(name_sql),
            )

        # extensions need a superuser, connected to the new database
        with admin_connection(name) as admin:
            for ext in EXTENSIONS:
                admin.execute(sql.SQL("CREATE EXTENSION {}").format(sql.Identifier(ext)))

    except Exception as e:
        drop(name)
        raise e

    return Sandbox(
        name=name,
        connection_info=make_conninfo(DATABASE_URL, dbname=name, user=name, password=password)
    )

def drop(name: str) -> None:
    name_sql = sql.Identifier(name)
    with admin_connection() as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name_sql))
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(name_sql))


def drop_all() -> list[str]:
    # for crashes etc
    with admin_connection() as admin:
        rows = admin.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'sqlscope\\_%' "
            "UNION SELECT rolname FROM pg_roles WHERE rolname LIKE 'sqlscope\\_%'"
        ).fetchall()
    names = [name for (name,) in rows]
    for name in names:
        drop(name)
    return names