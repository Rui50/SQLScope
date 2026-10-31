""" check a pasted schema before it gets near postgres """

from pglast import ast, parse_sql, split
from pglast.enums import AlterTableType
from pglast.parser import ParseError

# trusted ext, sandbox role can create these without being a superuser
ALLOWED_EXTENSIONS = {"btree_gin", "btree_gist", "citext", "hstore", "pg_trgm", "pgcrypto", "uuid-ossp"}

# statements that only define structure (a view is just a saved query)
ALLOWED_STATEMENTS = (ast.CreateEnumStmt, ast.IndexStmt, ast.CreateSeqStmt, ast.CommentStmt, ast.ViewStmt)


class SchemaError(Exception):
    """The schema can't be loaded; the message says why."""

def validate_schema(schema_sql: str) -> list[str]:
    # return the schema statements one by one, or raise SchemaError
    try:
        statements = split(schema_sql)
    except ParseError as e:
        raise SchemaError(f"Syntax error: {e}")

    if not statements:
        raise SchemaError("No SQL statements found")

    for statement in statements:
        check(parse_sql(statement)[0].stmt, statement)

    return list(statements)

def check(node: ast.Node, statement: str) -> None:
    if isinstance(node, ALLOWED_STATEMENTS):
        return

    if isinstance(node, ast.CreateStmt):
        schema = node.relation.schemaname
        if schema in (None, "public"):
            return
        raise SchemaError(f"Only the public schema is allowed, not {schema!r} in {statement!r}")

    if isinstance(node, ast.AlterTableStmt):
        if all(cmd.subtype == AlterTableType.AT_AddConstraint for cmd in node.cmds):
            return
        raise SchemaError(f"Only ALTER TABLE ... ADD CONSTRAINT is allowed:\n  {statement}")

    if isinstance(node, ast.CreateExtensionStmt):
        if node.extname in ALLOWED_EXTENSIONS:
            return
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise SchemaError(f"Extension {node.extname!r} is not allowed (allowed: {allowed})")

    raise SchemaError(f"{type(node).__name__} isn't supported yet:\n  {statement}")