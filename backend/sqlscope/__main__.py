"""python -m sqlscope so i dont forget"""

import argparse
from pathlib import Path

import psycopg

from sqlscope import sandbox, validate
from sqlscope.measure import measure_workload, sample_workload
from sqlscope.recommend import (
    build_indexes, choose_indexes, find_candidates, find_redundant, force_skipped, judge_indexes, screen_candidates,
)
from sqlscope.report import (
    print_comparison, print_fill, print_forced, print_indexes, print_measurements, print_recommendation, print_schema,
    print_screening, print_stats, print_workload,
)
from sqlscope.schema import read_schema, read_stats
from sqlscope.seed import DEFAULT_ROWS, SeedError, seed_data
from sqlscope.workload import build_workload

def run(
    schema_path: Path, keep: bool, rows: int, table_rows: dict[str, int], show_schema: bool, show_workload: bool
) -> None:
    # validate before creating anything, so bad input fails fast
    try:
        statements = validate.validate_schema(schema_path.read_text())
    except validate.SchemaError as e:
        raise SystemExit(f"Schema rejected: {e}")

    sb = sandbox.create_sandbox()
    print(f"Created sandbox {sb.name}")

    try:
        with psycopg.connect(sb.connection_info) as conn:
            # one at a time, so a failure shows which statement broke
            for statement in statements:
                try:
                    conn.execute(statement)
                except psycopg.Error as e:
                    raise SystemExit(f"Postgres rejected:\n  {statement}\n{e}")
            # ask postgres what it actually created
            tables = read_schema(conn)
            try:
                fills = seed_data(conn, tables, rows, table_rows)
            except (SeedError, psycopg.Error) as e:
                raise SystemExit(f"Can't generate data: {e}")
            stats = read_stats(conn)
            queries = build_workload(tables, {fill.table: fill.requested for fill in fills})
            samples = sample_workload(conn, queries)
            before = measure_workload(conn, queries, samples)
            screenings = screen_candidates(conn, find_candidates(queries, tables), samples)
            best = choose_indexes(screenings)
            built = build_indexes(conn, best)
            after = measure_workload(conn, queries, samples)
            forced = force_skipped(conn, screenings, built, after, samples)
            verdicts = judge_indexes(built, before, after)
            redundant = find_redundant(tables, verdicts)
        print(f"Loaded {len(tables)} tables\n")
        if show_schema:
            print_schema(tables)
        print_fill(fills)
        print_stats(tables, stats)
        if show_workload:
            print_workload(queries)
        print_measurements(before)
        print_screening(screenings)
        print_indexes(built, best)
        print_comparison(before, after, best)
        print_forced(forced)
        print_recommendation(verdicts, redundant)
    finally:
        if keep:
            print(f"Kept. Inspect with: docker compose exec db psql -U postgres -d {sb.name}")
        else:
            sandbox.drop(sb.name)
            print(f"Dropped sandbox {sb.name}")

def row_count_arg(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"expected a whole number above 0, got {value!r}")
    return int(value)

def table_rows_arg(value: str) -> tuple[str, int]:
    # "categories=50" -> ("categories", 50)
    name, _, count = value.partition("=")
    if not name:
        raise argparse.ArgumentTypeError(f"expected TABLE=N, got {value!r}")
    return name, row_count_arg(count)

def main() -> None:
    parser = argparse.ArgumentParser(prog="sqlscope")
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="load a schema into a fresh sandbox")
    run_cmd.add_argument("schema", type=Path, help="path to a .sql file")
    run_cmd.add_argument("--keep", action="store_true", help="don't drop the sandbox afterwards")
    run_cmd.add_argument("--rows", type=row_count_arg, default=DEFAULT_ROWS,
                         help="base row count per table (default %(default)s)")
    run_cmd.add_argument("--table-rows", type=table_rows_arg, action="append", default=[], metavar="TABLE=N",
                         help="row count for one table, can be repeated")
    run_cmd.add_argument("--show-schema", action="store_true", help="print the schema as postgres read it")
    run_cmd.add_argument("--show-workload", action="store_true", help="print the SQL of every generated query")

    commands.add_parser("cleanup", help="drop all leftover sandboxes")

    args = parser.parse_args()
    if args.command == "run":
        run(args.schema, args.keep, args.rows, dict(args.table_rows), args.show_schema, args.show_workload)
    elif args.command == "cleanup":
        dropped = sandbox.drop_all()
        print(f"Dropped {len(dropped)} sandbox(es)")



if __name__ == "__main__":
    main()