#!/usr/bin/env python3
"""Phase 0 driver: convert the mattermost-looker LookML project to Cube (best-effort)
and produce a model-complexity report.

Self-contained on purpose: instead of patching lkml2cube, we fix its non-recursive
include glob at runtime. lkml2cube's file_loader calls ``glob.glob(pattern)`` without
``recursive=True``, so include patterns like ``/**/**/*.view.lkml`` silently miss
deeply nested views (e.g. ``views/marts/product/``). We force recursion below before
importing lkml2cube.

Run with Python 3.11+ that has the deps from cube/requirements.txt:
    python3.11 cube/scripts/convert.py
"""

from __future__ import annotations

import glob as _glob_mod

# --- Self-contained fix: force lkml2cube's include globs to recurse -----------
_orig_glob = _glob_mod.glob


def _recursive_glob(pathname, *args, **kwargs):
    kwargs.setdefault("recursive", True)
    return _orig_glob(pathname, *args, **kwargs)


_glob_mod.glob = _recursive_glob  # noqa: must run before lkml2cube import

import io  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
from collections import Counter  # noqa: E402
from contextlib import redirect_stdout, redirect_stderr  # noqa: E402
from pathlib import Path  # noqa: E402

from lkml2cube.parser.explores import parse_explores  # noqa: E402
from lkml2cube.parser.loader import file_loader  # noqa: E402
from lkml2cube.parser.views import parse_view  # noqa: E402
from lkml2cube.parser import loader as _loader  # noqa: E402
from lkml2cube.parser import types as _types  # noqa: E402

# --- Self-contained fix #2: lkml2cube's types.Console.print(self, s, *args) rejects
# keyword args, but its own error/warning paths pass style="bold red". That makes a
# per-view/explore warning crash the whole parse instead of skipping the bad item.
# Swallow extra args/kwargs so best-effort parsing continues. (Upstream PR candidate.)
_types.console.print = lambda message="", *args, **kwargs: print(message)

import yaml  # noqa: E402

# Match lkml2cube's YAML formatting for multi-line SQL (folded/literal scalars).
yaml.add_representer(_types.folded_unicode, _types.folded_unicode_representer)
yaml.add_representer(_types.literal_unicode, _types.literal_unicode_representer)

REPO = Path(__file__).resolve().parents[2]
CUBE_DIR = REPO / "cube"
MODEL_DIR = CUBE_DIR / "model"
REPORTS_DIR = CUBE_DIR / "reports"
MANIFEST = REPO / "manifest.lkml"


def discover_models() -> list[Path]:
    return sorted(p for p in REPO.rglob("*.model.lkml") if CUBE_DIR not in p.parents)


def load_namespace(models: list[Path]) -> tuple[dict, str]:
    """Load manifest + all model files (and their included views) into one namespace.

    Returns the merged namespace and any captured loader warnings (e.g. unsupported keys).
    """
    _loader.visited_path.clear()
    buf = io.StringIO()
    namespace: dict | None = None
    with redirect_stdout(buf), redirect_stderr(buf):
        if MANIFEST.exists():
            namespace = file_loader(str(MANIFEST), str(REPO), namespace=namespace)
        for model in models:
            namespace = file_loader(str(model), str(REPO), namespace=namespace)
    return (namespace or {}), buf.getvalue()


def schema_of(sql_table_name: str) -> str | None:
    s = sql_table_name.strip().rstrip(";").strip()
    if not s:
        return None
    if "{%" in s or "{{" in s:
        return "(dynamic/liquid)"
    parts = [p.strip().strip('"').strip("`") for p in s.split(".")]
    return parts[0].upper() if len(parts) >= 2 else None


def count_source(namespace: dict) -> dict:
    views = namespace.get("views", []) or []
    explores = namespace.get("explores", []) or []

    field_keys = ("dimensions", "dimension_groups", "measures", "filters", "parameters")
    field_counts = {k: 0 for k in field_keys}
    schemas: Counter = Counter()
    tables: set[str] = set()
    derived = 0

    for v in views:
        for k in field_keys:
            field_counts[k] += len(v.get(k, []) or [])
        stn = v.get("sql_table_name")
        if stn:
            tables.add(stn.strip().rstrip(";").strip())
            sc = schema_of(stn)
            if sc:
                schemas[sc] += 1
        if "derived_table" in v:
            derived += 1

    joins = sum(len(e.get("joins", []) or []) for e in explores)

    return {
        "views": len(views),
        "explores": len(explores),
        "joins": joins,
        "fields": field_counts,
        "total_fields": sum(field_counts.values()),
        "schemas": schemas,
        "distinct_tables": len(tables),
        "derived_table_views": derived,
    }


def scan_unsupported() -> dict:
    patterns = {
        "liquid_templating": re.compile(r"\{\%|\{\{"),
        "derived_table": re.compile(r"\bderived_table\s*:"),
        "extends": re.compile(r"\bextends\s*:"),
        "extension_required": re.compile(r"\bextension\s*:\s*required"),
        "refinements": re.compile(r"(?m)^\s*\+\w"),
    }
    hits: dict[str, dict] = {k: {"files": 0, "occurrences": 0, "examples": []} for k in patterns}
    for path in REPO.rglob("*.lkml"):
        if CUBE_DIR in path.parents:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, rx in patterns.items():
            found = rx.findall(text)
            if found:
                hits[name]["files"] += 1
                hits[name]["occurrences"] += len(found)
                if len(hits[name]["examples"]) < 5:
                    hits[name]["examples"].append(str(path.relative_to(REPO)))
    return hits


def generate_cube(namespace: dict) -> tuple[dict | None, str, str]:
    """Best-effort Cube generation. Try full explores->cubes+views; fall back to cubes-only.

    Returns (cube_def, path_description, captured_warnings).
    """
    buf = io.StringIO()
    cube_def: dict | None = None
    path = ""
    with redirect_stdout(buf), redirect_stderr(buf):
        try:
            cube_def = parse_explores(dict(namespace))
            path = "parse_explores (cubes + joins + views)"
        except Exception as e:  # noqa: BLE001 - best-effort, capture and fall back
            err = f"parse_explores failed: {type(e).__name__}: {e}"
            try:
                cube_def = parse_view(dict(namespace))
                path = f"fallback parse_view (cubes only); {err}"
            except Exception as e2:  # noqa: BLE001
                path = f"{err}; parse_view also failed: {type(e2).__name__}: {e2}"
    return cube_def, path, buf.getvalue()


def dedupe_by_name(items: list[dict]) -> tuple[list[dict], int]:
    """Keep first occurrence per 'name'; return (unique_items, num_duplicates)."""
    seen: dict[str, dict] = {}
    dups = 0
    for it in items or []:
        n = it.get("name")
        if n in seen:
            dups += 1
            continue
        seen[n] = it
    return list(seen.values()), dups


def write_model(cube_def: dict, outdir: Path) -> dict:
    """Write cube_def to outdir/cubes and outdir/views, one YAML file per element.

    Sanitizes file names (some generated names contain '/') without altering the model
    'name' fields, so joins/references between cubes stay intact.
    """
    summary = {"cubes": 0, "views": 0}
    for kind in ("cubes", "views"):
        items = cube_def.get(kind, []) or []
        if not items:
            continue
        d = outdir / kind
        d.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()  # lowercased: macOS/Windows filesystems are case-insensitive
        for item in items:
            name = str(item.get("name", "unnamed"))
            base = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            fname = base
            i = 2
            while fname.lower() in used:  # distinct names that map to the same file
                fname = f"{base}__{i}"
                i += 1
            used.add(fname.lower())
            (d / (fname + ".yml")).write_text(
                yaml.dump({kind: [item]}, allow_unicode=True), encoding="utf-8"
            )
            summary[kind] += 1
    return summary


def md_table(rows: list[tuple[str, object]]) -> str:
    lines = ["| Metric | Value |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines)


def write_report(src: dict, gen: dict, unsupported: dict) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    f = src["fields"]
    schema_lines = "\n".join(
        f"| {name} | {cnt} |" for name, cnt in src["schemas"].most_common()
    )
    unsup_lines = "\n".join(
        f"| {name} | {d['files']} | {d['occurrences']} | {', '.join(d['examples'])} |"
        for name, d in unsupported.items()
    )
    report = f"""# Mattermost-Looker -> Cube: model complexity report

Generated by `cube/scripts/convert.py` (Phase 0). Counts are measured from the parsed
LookML namespace (manifest + all `*.model.lkml` + included views, with recursive include
resolution).

## Source LookML size

{md_table([
    ("Model files (.model.lkml)", src["model_files"]),
    ("Views", src["views"]),
    ("Explores", src["explores"]),
    ("Join definitions (across explores)", src["joins"]),
    ("Dimensions", f["dimensions"]),
    ("Dimension groups", f["dimension_groups"]),
    ("Measures", f["measures"]),
    ("Filters", f["filters"]),
    ("Parameters", f["parameters"]),
    ("Total fields", src["total_fields"]),
])}

## Warehouse breadth (what must be synthesized for Phase 1)

{md_table([
    ("Distinct source tables (sql_table_name)", src["distinct_tables"]),
    ("Distinct top-level schemas/databases", len(src["schemas"])),
    ("Views backed by a derived_table (no base table)", src["derived_table_views"]),
])}

### Views per source schema/database

| Schema/DB | Views |
| --- | --- |
{schema_lines}

## Generated Cube model (best-effort, Phase 0)

{md_table([
    ("Generation path", gen["path"]),
    ("Cubes generated (unique)", gen["cubes"]),
    ("Cube views generated (unique)", gen["views"]),
    ("Duplicate cube names collapsed", gen.get("dupes_cubes", 0)),
    ("Duplicate view names collapsed", gen.get("dupes_views", 0)),
    ("Views that errored during generation (skipped)", gen.get("view_errors", 0)),
    ("Explores that errored during generation (skipped)", gen.get("explore_errors", 0)),
    ("Output", "`cube/model/cubes/`, `cube/model/views/`"),
])}

> See `cube/reports/generation.log` for the per-view/explore errors (mostly Liquid/HTML
> measures and unreachable join paths) that were skipped in this best-effort pass.

> Phase 0 is best-effort: the output is not yet runnable. Phase 1 makes it runnable on
> DuckDB + Parquet (dialect rewrite + resolving the unsupported constructs below).

## Unsupported / hard-to-convert LookML (Phase 1 work)

| Construct | Files | Occurrences | Examples |
| --- | --- | --- | --- |
{unsup_lines}

- Liquid templating: passed through verbatim by lkml2cube, so SQL won't run until translated.
- Derived tables: limited handling; become Cube `sql` cubes / materialized views.
- `extends` / refinements / `extension: required`: explore/view inheritance to flatten into Cube.
"""
    out = REPORTS_DIR / "complexity.md"
    out.write_text(report, encoding="utf-8")
    return out


def main() -> None:
    models = discover_models()
    namespace, warnings = load_namespace(models)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / "unsupported.log").write_text(warnings, encoding="utf-8")

    src = count_source(namespace)
    src["model_files"] = len(models)
    unsupported = scan_unsupported()

    cube_def, path, gen_warnings = generate_cube(namespace)
    (REPORTS_DIR / "generation.log").write_text(gen_warnings, encoding="utf-8")
    gen = {
        "path": path, "cubes": 0, "views": 0, "dupes_cubes": 0, "dupes_views": 0,
        "view_errors": gen_warnings.count("Error while parsing view:"),
        "explore_errors": gen_warnings.count("Error while parsing explore:"),
    }
    if cube_def:
        cubes, gen["dupes_cubes"] = dedupe_by_name(cube_def.get("cubes", []))
        views, gen["dupes_views"] = dedupe_by_name(cube_def.get("views", []))
        cube_def["cubes"], cube_def["views"] = cubes, views
        if MODEL_DIR.exists():
            shutil.rmtree(MODEL_DIR)
        written = write_model(cube_def, MODEL_DIR)
        gen["cubes"] = written["cubes"]
        gen["views"] = written["views"]

    report_path = write_report(src, gen, unsupported)

    print("=== Phase 0 conversion summary ===")
    print(f"models={src['model_files']} views={src['views']} explores={src['explores']} "
          f"joins={src['joins']} total_fields={src['total_fields']}")
    print(f"distinct_tables={src['distinct_tables']} schemas={len(src['schemas'])} "
          f"derived_table_views={src['derived_table_views']}")
    print(f"generated: cubes={gen['cubes']} cube_views={gen['views']} "
          f"(dupes collapsed: {gen['dupes_cubes']} cubes / {gen['dupes_views']} views) via {gen['path']}")
    print(f"generation errors skipped: {gen['view_errors']} views / {gen['explore_errors']} explores")
    print(f"report: {report_path.relative_to(REPO)}")
    print(f"warnings captured: {len(warnings.splitlines())} lines -> cube/reports/unsupported.log")


if __name__ == "__main__":
    main()
