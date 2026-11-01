"""report output"""

from rich.console import Console
from rich.markup import escape
from rich.table import Table as RichTable
from rich.text import Text

from sqlscope.measure import Measurement
from sqlscope.recommend import BuiltIndex, Candidate, Estimate, Screening
from sqlscope.schema import ColumnStats, Table
from sqlscope.seed import TableFill
from sqlscope.workload import Query

console = Console()

# what the "pages" column counts (EXPLAIN's BUFFERS), shown under the tables that have it
PAGES_CAPTION = "pages = 8 KB page visits: the same page visited twice counts twice"


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
    out = RichTable(
        title="data shape (what the planner sees)",
        caption="correlation: 1 = values follow the order rows are stored in, 0 = scattered",
        title_justify="left",
        caption_justify="left",
        show_edge=False,
    )
    out.add_column("column")
    out.add_column("distinct", justify="right")
    out.add_column("null", justify="right")
    out.add_column("correlation", justify="right")
    out.add_column("most common")

    for table in tables.values():
        for col in table.columns.values():
            s = stats.get((table.name, col.name))
            if s is None:
                continue
            top = zip(s.common_values[:3], s.common_freqs[:3])
            common = " · ".join(f"{escape(value[:20])} {freq:.0%}" for value, freq in top)
            null = f"{s.null_frac:.0%}" if s.null_frac else ""
            correlation = f"{s.correlation:.2f}" if s.correlation is not None else ""
            out.add_row(f"{table.name}.{col.name}", f"{s.distinct:,.0f}", null, correlation, common)

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
    out = RichTable(
        title="baseline", caption=PAGES_CAPTION, title_justify="left", caption_justify="left", show_edge=False
    )
    out.add_column("query", no_wrap=True)
    out.add_column("plan")
    out.add_column("rows", justify="right")
    out.add_column("time", justify="right")
    out.add_column("pages", justify="right")

    for m in measurements:
        if m.skipped:
            out.add_row(escape(m.query.name), Text(f"skipped: {m.skipped}", style="dim"), "", "", "")
            continue
        out.add_row(escape(m.query.name), plan_text(m), f"{m.rows:,}", f"{m.ms:.2f} ms", f"{m.pages:,}")

    console.print(out)


# a measurement's plan, Seq Scans in red; when different values got different plans, each plan
# with how often it came up and one value that got it
def plan_text(m: Measurement) -> Text:
    if len(m.plans) == 1:
        plan = next(iter(m.plans))
    else:
        parts = []
        for summary, values in sorted(m.plans.items(), key=lambda item: -len(item[1])):
            example = f", e.g. {m.query.param[1]} = {values[0]}" if m.query.param else ""
            parts.append(f"{summary} ({len(values)}×{example})")
        plan = " | ".join(parts)
    return Text(plan, style="red" if "Seq Scan" in plan else "")


# each candidate index, the queries it could help, and the planner's estimate with it (hypopg)
def print_screening(screenings: list[Screening]) -> None:
    out = RichTable(title="candidate indexes (planner estimates with hypopg)", title_justify="left", show_edge=False)
    out.add_column("index", no_wrap=True)
    out.add_column("query", no_wrap=True)
    out.add_column("est. cost", justify="right")
    out.add_column("would use it?")

    for screening in screenings:
        index = f"{screening.candidate.table} ({', '.join(screening.candidate.columns)})"
        for e in screening.estimates:
            cost = f"{e.cost_before:,.0f} → {e.cost_after:,.0f}"
            if e.used:
                verdict = Text(f"yes ({e.used}/{e.runs})", style="green")
            else:
                verdict = Text(f"no, keeps {e.plan_before}", style="dim")
            out.add_row(index, escape(e.query.name), cost, verdict)
            index = ""  # the index name only on its first line

    console.print(out)


# the indexes that were built: name, size on disk, and the queries that chose them
def print_indexes(built: list[BuiltIndex], best: dict[str, tuple[Candidate, Estimate]]) -> None:
    out = RichTable(title="recommended indexes (built)", title_justify="left", show_edge=False)
    out.add_column("index", no_wrap=True)
    out.add_column("size", justify="right")
    out.add_column("chosen for")

    for index in built:
        queries = [name for name, (candidate, _) in best.items() if candidate is index.candidate]
        out.add_row(escape(index.name), size_text(index.size), escape(", ".join(queries)))

    console.print(out)


# every query before and after the indexes: plan now, pages and time, and the planner's predicted
# gain next to the measured ones. Both pages and time: matching rows spread over many pages can get
# much faster to find without reading fewer pages
def print_comparison(
    before: list[Measurement], after: list[Measurement], best: dict[str, tuple[Candidate, Estimate]]
) -> None:
    out = RichTable(
        title="before → after", caption=PAGES_CAPTION, title_justify="left", caption_justify="left", show_edge=False
    )
    out.add_column("query", no_wrap=True)
    out.add_column("plan now")
    out.add_column("pages", justify="right")
    out.add_column("time", justify="right")
    out.add_column("gain: est. · pages · time", justify="right")

    for b, a in zip(before, after):
        if b.skipped:
            out.add_row(escape(b.query.name), Text(f"skipped: {b.skipped}", style="dim"), "", "", "")
            continue
        style = "green" if a.pages < b.pages else "yellow" if a.pages > b.pages else ""
        pages = Text(f"{b.pages:,} → {a.pages:,}", style=style)
        time = f"{b.ms:.2f} → {a.ms:.2f} ms"
        gain = ""
        if b.query.name in best:
            estimate = best[b.query.name][1]
            gain = " · ".join([
                ratio(estimate.cost_before, estimate.cost_after), ratio(b.pages, a.pages), ratio(b.ms, a.ms)
            ])
        out.add_row(escape(b.query.name), plan_text(a), pages, time, gain)

    console.print(out)


# how many times smaller after is than before: 58×, 2.4×
def ratio(before: float, after: float) -> str:
    times = before / max(after, 0.001)
    return f"{times:.0f}×" if times >= 10 else f"{times:.1f}×"


# bytes as KB or MB
def size_text(size: int) -> str:
    return f"{size / 1024:,.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
