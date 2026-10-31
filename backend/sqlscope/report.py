"""report output"""

from rich.console import Console
from rich.markup import escape
from rich.table import Table as RichTable
from rich.text import Text

from sqlscope.measure import Measurement
from sqlscope.schema import ColumnStats, Table
from sqlscope.seed import TableFill
from sqlscope.workload import Query

console = Console()


def print_schema(tables: dict[str, Table]) -> None:
    for table in tables.values():
        out = RichTable(title=table.name, title_justify="left", show_edge=False)
        out.add_column("column")
        out.add_column("type")
        out.add_column("notes")

        fks = {fk.columns[0]: fk for fk in table.foreign_keys if len(fk.columns) == 1}
        for col in table.columns.values():
            notes = []
            if col.name in table.primary_key:
                notes.append("PK")
            if [col.name] in table.unique:
                notes.append("unique")
            if col.name in fks:
                fk = fks[col.name]
                notes.append(f"→ {fk.ref_table}.{fk.ref_columns[0]}")
            if not col.nullable:
                notes.append("not null")
            if col.identity:
                notes.append(f"identity ({col.identity})")
            elif col.default:
                notes.append(f"default {col.default}")
            if col.enum_values:
                notes.append("enum: " + ", ".join(col.enum_values))
            notes += [f"check {c}" for c in col.checks]
            out.add_row(col.name, escape(col.type), escape(", ".join(notes)))

        console.print(out)
        for check in table.checks:
            console.print(f"  [dim]check[/dim] {escape(check)}")
        for index in table.indexes:
            console.print(f"  [dim]index[/dim] {escape(index.definition)}")
        console.print()


def print_fill(fills: list[TableFill]) -> None:
    out = RichTable(title="generated data", title_justify="left", show_edge=False)
    out.add_column("table")
    out.add_column("rows", justify="right")
    out.add_column("time", justify="right")

    for fill in fills:
        rows = f"{fill.inserted:,}"
        if fill.inserted < fill.requested:
            rows += f" [dim](of {fill.requested:,})[/dim]"
        out.add_row(fill.table, rows, f"{fill.seconds:.2f}s")

    console.print(out)


# what the planner has about each column: how many different values, NULLs, most common
def print_stats(tables: dict[str, Table], stats: dict[tuple[str, str], ColumnStats]) -> None:
    out = RichTable(title="data shape (what the planner sees)", title_justify="left", show_edge=False)
    out.add_column("column")
    out.add_column("distinct", justify="right")
    out.add_column("null", justify="right")
    out.add_column("most common")

    for table in tables.values():
        for col in table.columns.values():
            s = stats.get((table.name, col.name))
            if s is None:
                continue
            top = zip(s.common_values[:3], s.common_freqs[:3])
            common = " · ".join(f"{escape(value[:20])} {freq:.0%}" for value, freq in top)
            null = f"{s.null_frac:.0%}" if s.null_frac else ""
            out.add_row(f"{table.name}.{col.name}", f"{s.distinct:,.0f}", null, common)

    console.print(out)



# the generated queries, one per line ($1 marks where a sampled value goes)
def print_workload(queries: list[Query]) -> None:
    out = RichTable(title=f"workload ({len(queries)} queries)", title_justify="left", show_edge=False)
    out.add_column("query", no_wrap=True)
    out.add_column("sql", no_wrap=True, overflow="ellipsis", max_width=80)

    for query in queries:
        text = query.sql.as_string().replace("%s", "$1")
        out.add_row(escape(query.name), escape(text))

    console.print(out)


# one row per query: plan, rows, median time and pages; Seq Scans in red, they're the index candidates
def print_measurements(measurements: list[Measurement]) -> None:
    out = RichTable(title="baseline", title_justify="left", show_edge=False)
    out.add_column("query", no_wrap=True)
    out.add_column("plan")
    out.add_column("rows", justify="right")
    out.add_column("time", justify="right")
    out.add_column("pages", justify="right")

    for m in measurements:
        if m.skipped:
            out.add_row(escape(m.query.name), Text(f"skipped: {m.skipped}", style="dim"), "", "", "")
            continue
        plan = " | ".join(m.plans)
        style = "red" if "Seq Scan" in plan else ""
        out.add_row(escape(m.query.name), Text(plan, style=style), f"{m.rows:,}", f"{m.ms:.2f} ms", f"{m.pages:,}")

    console.print(out)
