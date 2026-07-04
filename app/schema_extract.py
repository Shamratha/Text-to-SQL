"""Automatic schema introspection for the prompt engine.

Extracts tables, columns + types, PK/FK relationships, and sample values for
low-cardinality text columns. Also provides a lightweight relevance filter so
large schemas don't blow up the prompt.
"""
import re
from dataclasses import dataclass, field

import duckdb


@dataclass
class Column:
    name: str
    dtype: str
    samples: list[str] = field(default_factory=list)


@dataclass
class Table:
    name: str
    columns: list[Column]
    primary_keys: list[str]
    foreign_keys: list[str]  # human-readable, e.g. "customer_id -> customers.customer_id"
    row_count: int


FK_RE = re.compile(r"FOREIGN KEY\s*\((.+?)\)\s*REFERENCES\s+(\w+)\s*\((.+?)\)", re.IGNORECASE)


def extract_schema(con: duckdb.DuckDBPyConnection, sample_max_distinct: int = 20) -> dict[str, Table]:
    tables: dict[str, Table] = {}
    for (tname,) in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall():
        cols = [
            Column(name=c, dtype=t)
            for c, t in con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position", [tname]
            ).fetchall()
        ]
        pks: list[str] = []
        fks: list[str] = []
        for ctype, ctext, colnames in con.execute(
            "SELECT constraint_type, constraint_text, constraint_column_names "
            "FROM duckdb_constraints() WHERE table_name = ?", [tname]
        ).fetchall():
            if ctype == "PRIMARY KEY":
                pks.extend(colnames)
            elif ctype == "FOREIGN KEY":
                m = FK_RE.search(ctext or "")
                if m:
                    fks.append(f"{m.group(1).strip()} -> {m.group(2)}.{m.group(3).strip()}")

        # Sample values for low-cardinality VARCHAR columns (disambiguation context)
        for col in cols:
            if "VARCHAR" not in col.dtype.upper():
                continue
            (n_distinct,) = con.execute(
                f'SELECT COUNT(DISTINCT "{col.name}") FROM "{tname}"'
            ).fetchone()
            if 0 < n_distinct <= sample_max_distinct:
                col.samples = [
                    str(v) for (v,) in con.execute(
                        f'SELECT DISTINCT "{col.name}" FROM "{tname}" '
                        f'WHERE "{col.name}" IS NOT NULL ORDER BY 1 LIMIT {sample_max_distinct}'
                    ).fetchall()
                ]

        (row_count,) = con.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()
        tables[tname] = Table(tname, cols, pks, fks, row_count)
    return tables


def filter_relevant_tables(tables: dict[str, Table], question: str,
                           always_include_if_at_most: int = 8) -> dict[str, Table]:
    """Keyword-overlap relevance filter. Small schemas are passed through whole;
    for large ones, keep tables whose name/columns overlap the question, plus
    any table reachable via a foreign key from a kept table (JOIN paths)."""
    if len(tables) <= always_include_if_at_most:
        return tables

    tokens = set(re.findall(r"[a-z]+", question.lower()))
    scored: dict[str, int] = {}
    for name, t in tables.items():
        words = set(re.findall(r"[a-z]+", name.lower()))
        for c in t.columns:
            words |= set(re.findall(r"[a-z]+", c.name.lower()))
        # crude singular/plural bridging
        expanded = words | {w + "s" for w in words} | {w.rstrip("s") for w in words}
        scored[name] = len(tokens & expanded)

    kept = {n for n, s in scored.items() if s > 0}
    # pull in FK neighbours so JOINs remain possible
    for n in list(kept):
        for fk in tables[n].foreign_keys:
            ref = fk.split("->")[1].strip().split(".")[0]
            kept.add(ref)
    if not kept:  # nothing matched — fall back to full schema
        return tables
    return {n: t for n, t in tables.items() if n in kept}


def schema_to_markdown(tables: dict[str, Table]) -> str:
    """Render the schema as compact markdown for the LLM prompt."""
    parts = []
    for t in tables.values():
        lines = [f"### {t.name}  ({t.row_count} rows)"]
        for c in t.columns:
            tags = []
            if c.name in t.primary_keys:
                tags.append("PK")
            desc = f"- {c.name} {c.dtype}"
            if tags:
                desc += f" [{', '.join(tags)}]"
            if c.samples:
                desc += f"  values: {', '.join(c.samples[:12])}"
            lines.append(desc)
        for fk in t.foreign_keys:
            lines.append(f"- FK: {fk}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def schema_to_dict(tables: dict[str, Table]) -> dict:
    return {
        name: {
            "columns": [{"name": c.name, "type": c.dtype, "samples": c.samples} for c in t.columns],
            "primary_keys": t.primary_keys,
            "foreign_keys": t.foreign_keys,
            "row_count": t.row_count,
        }
        for name, t in tables.items()
    }
