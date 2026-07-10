"""
storage.py — local parquet warehouse + manifest + DuckDB catalog.

Layout (all under config.DATA_ROOT, LOCAL on C:, never synced to Drive):
    raw/options/{SYMBOL}/{YYYYMMDD}.parquet   one file per root per trading day
    raw/options/_manifest.json                {SYMBOL: {YYYYMMDD: rows}}
    catalog.duckdb                            a view over all the parquet

One file per (symbol, day) because the ThetaData EOD endpoints require requesting
expiration=* a single day at a time — so the natural unit of work, of resumability,
and of the forward IBKR collector is one trading day. A present file (even 0-row,
for a market holiday) means "done — skip". Parquet is zstd-compressed and DuckDB
reads the whole tree with one glob.
"""

from __future__ import annotations

import json
import os

import pandas as pd

import config


def _manifest() -> dict:
    if not config.MANIFEST.exists():
        return {}
    try:
        return json.loads(config.MANIFEST.read_text())
    except (json.JSONDecodeError, OSError):
        # A truncated/corrupt manifest (e.g. a kill mid-write before atomic writes
        # existed) must NOT brick the grab. Treat as empty — have_day() keys off
        # file presence, not the manifest, so nothing gets needlessly re-pulled.
        return {}


def _save_manifest(m: dict) -> None:
    config.MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: write to a temp file then replace, so a kill mid-write can never
    # leave a torn manifest that would fail json.loads on every later write_day.
    tmp = config.MANIFEST.with_name(config.MANIFEST.name + ".tmp")
    tmp.write_text(json.dumps(m, indent=2, sort_keys=True))
    os.replace(tmp, config.MANIFEST)


def partition_path(symbol: str, daystr: str):
    return config.RAW_OPTIONS / symbol / f"{daystr}.parquet"


def have_day(symbol: str, daystr: str) -> bool:
    """True if this (symbol, day) is already on disk (file present = done)."""
    return partition_path(symbol, daystr).exists()


def write_day(symbol: str, daystr: str, df: pd.DataFrame) -> int:
    """Write one (symbol, day) parquet (zstd) and record the row count. 0-row OK.

    The parquet is written atomically (temp file + os.replace). The supervisor
    kills a stalled download with terminate()->kill(); without atomicity a kill
    landing mid-write would leave a torn .parquet that have_day() still counts as
    "done" — a permanent, silently-corrupt hole. The .tmp name does not match the
    have_day()/catalog globs, so an orphaned temp (kill between write and replace)
    is harmless and is overwritten when that same day is re-pulled.
    """
    p = partition_path(symbol, daystr)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    df.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False)
    os.replace(tmp, p)
    m = _manifest()
    m.setdefault(symbol, {})[daystr] = int(len(df))
    _save_manifest(m)
    return len(df)


def _nonempty_parquets() -> list[str]:
    """Forward-slashed paths of every parquet that actually has columns.

    Many files are zero-column "no-data-day" markers (a day the EOD endpoint
    returned nothing). DuckDB 1.5.4's read_parquet(union_by_name=true) REFUSES to
    scan a zero-column file, so the catalog view must be built over only the files
    that carry a schema. We keep the empty markers on disk untouched — have_day()
    relies on them so the collector won't re-pull those days — we just exclude them
    from the view. Reading only the parquet footer (num_columns) is cheap.

    Kept for backward compatibility / one-off diagnostics (e.g. a from-scratch
    audit). The incremental rebuild_catalog() below does NOT call this — it does
    its own cheap-mtime-scan + classify-only-what's-dirty, which is the whole
    point of the incremental design (see rebuild_catalog's docstring).
    """
    import pyarrow.parquet as pq

    kept: list[str] = []
    for f in config.RAW_OPTIONS.glob("*/*.parquet"):
        try:
            if pq.read_metadata(f).num_columns > 0:
                kept.append(str(f).replace("\\", "/"))
        except Exception:
            # An unreadable/corrupt footer is treated as "not includable" rather
            # than blowing up the whole rebuild.
            continue
    return kept


# --------------------------------------------------------------------------- #
# Incremental catalog rebuild
# --------------------------------------------------------------------------- #
# BACKGROUND (2026-07-10): the old rebuild_catalog() re-scanned metadata for ALL
# ~312k warehouse files AND re-embedded ALL of them as one literal DuckDB list in
# a single CREATE VIEW every time ANY script called it. A real production run was
# killed after 52 minutes with no completion. Diagnosis (real numbers, throwaway
# .duckdb files, isolated from the production catalog):
#   * step 1 (_nonempty_parquets(): pyarrow read_metadata() over every file) is
#     ~4-7 minutes for the full warehouse (~1-1.4ms/file) — NOT the dominant cost.
#   * step 2 (the single CREATE VIEW ... read_parquet([N paths], union_by_name=
#     true)) is the dominant cost, and scales far worse than linearly: measured
#     0.48s @ 2,000 files, 8.67s @ 20,000 files, >4.5 minutes AND CLIMBING (killed,
#     never finished) @ 100,000 files, while the SAME query with union_by_name
#     REMOVED took 0.02s / 0.15s / 0.74s at those same scales (a >350x gap at
#     100k, and growing) — union_by_name's schema-reconciliation over one giant
#     literal-list is the dominant + superlinear cost, not per-file pyarrow
#     metadata reads or SQL text size alone. This is consistent with prior
#     production experience: eod_daily.py's commit message (2026-06-27, ~102k
#     files at the time) already documented storage.rebuild_catalog() "hard-
#     crash[ing] the interpreter" (PyEval_SaveThread/GIL) — a giant literal-list
#     CREATE VIEW with union_by_name is both a slowness risk AND (independently
#     observed) a crash risk in this DuckDB version. Chunking the literal list
#     bounds both.
#   * Schema check (step 2 of the investigation): sampled ~1,190 real non-empty
#     files spanning every (symbol, year) bucket in the warehouse — column NAMES
#     and ORDER are identical everywhere (41 cols, matches ibkr_forward.py's
#     docstring claim), but column TYPES are not: open_interest is `double` in
#     some files and `int64` in others. union_by_name is therefore kept ON (it
#     reconciles by name, not by position) — the type drift is real, so dropping
#     it for speed alone would be a correctness gamble not worth taking here.
#
# FIX: split the view into small, mostly-frozen CHUNK views instead of one
# monolithic view over every file:
#   * Every non-empty file is assigned to exactly one chunk (`_catalog_chunks`
#     tracks chunk sizes; `_catalog_manifest` tracks path -> chunk_id + classification).
#   * A chunk holds at most CATALOG_CHUNK_SIZE files. Once full it is SEALED and
#     its DuckDB view (`_eod_chunk_<n>`) is never rebuilt again — it is only ever
#     referenced by name in the top-level UNION.
#   * Each rebuild only touches: (a) files that are brand new or whose on-disk
#     mtime changed since they were last classified (a cheap os.stat-based scan,
#     NOT a pyarrow metadata read, over the whole tree — seconds, not minutes),
#     and (b) the chunk(s) those dirty files land in. In the steady-state case
#     (a normal day: a handful of new files, maybe a rare repull) that means
#     rebuilding ONE open chunk of at most CATALOG_CHUNK_SIZE files, not 312k.
#   * The final `options_eod` view is `UNION ALL BY NAME` over every chunk view —
#     cheap regardless of total file count, since its SQL text only lists view
#     names, never raw file paths.
#
# CORRECTNESS under the warehouse's real write patterns (see write_day()'s
# atomic-replace-on-rewrite + the one-off repull_*.py scripts that DO overwrite
# an already-cataloged day in place):
#   * A DuckDB VIEW is not materialized — `SELECT * FROM options_eod` always
#     re-reads whatever bytes are on disk RIGHT NOW for every path in its
#     definition. So a file whose CONTENT changes after being cataloged (a
#     repull) needs NO rebuild at all — the next query already sees the new
#     bytes. Only the SET OF PATHS in a chunk's view ever needs to change.
#   * A file discovered to have changed (mtime dirty) whose empty/non-empty
#     classification is UNCHANGED needs no path-list change — nothing to do.
#   * A file that flips empty -> non-empty (a repull that fills in a former
#     zero-column marker with real data) is newly ADDED to the currently-open
#     chunk (never seen before by chunk-membership, regardless of how long the
#     path existed on disk).
#   * A file that flips non-empty -> empty (a rewrite that turns a previously
#     cataloged day back into a zero-column marker — not part of any normal
#     flow today, but a single 0-column file in a chunk's list would make
#     read_parquet(..., union_by_name=true) refuse to scan THAT WHOLE CHUNK) is
#     explicitly removed from its chunk's path list and that chunk is rebuilt
#     (bounded to CATALOG_CHUNK_SIZE files) — never silently left in a broken
#     state.
#   * Backfills that write PAST dates out of order (e.g. a 2026-07-09 file
#     written after 2026-07-10 already existed) are handled the same as any
#     other new file: membership is keyed off "path not yet classified /
#     mtime changed", never off a date cutoff — order of arrival doesn't matter.
#
# The very first rebuild against a fresh/rebuilt manifest still has to classify
# and chunk EVERY file once (a one-time bootstrap cost, same total work as
# before) — the payoff is every subsequent call being cheap and bounded.
#
# BACKGROUND (2026-07-10, bug #4 — found running the real bootstrap for the
# first time after fixing bugs #1-#3): the chunking design above bounds the
# cost of building EACH chunk's own view (confirmed cheap: 3-8s @ 5,000 real
# files), but the belief that the FINAL `UNION ALL BY NAME` over chunk views
# is "cheap regardless of file count, since its SQL text only lists view
# names" was WRONG and disproven with real numbers. DuckDB does not cache a
# referenced view's resolved column schema — creating (or even just
# executing) a query that references a view re-binds that view's full
# definition, and since each chunk view's definition is itself a
# `read_parquet([5,000 paths], union_by_name=true)`, unioning N chunk views
# re-triggers N chunks' worth of union_by_name schema reconciliation, and this
# compounds: measured real cost of `UNION ALL BY NAME` over already-built real
# chunk views was 4.06s @ 2 chunks, 10.44s @ 4, 34.15s @ 8, 139.91s @ 16 —
# each doubling of chunk count roughly QUADRUPLING the time (~O(n^2) in
# chunk count), independent of whether the union was BY NAME or positional,
# and independent of whether the nested scans went through named views or
# were inlined directly. Extrapolated (and consistent with what was actually
# observed before being killed) to ~30 min at the real 58-chunk count — this,
# not the per-chunk build step, was the actual wall the 46+/52-minute killed
# runs were hitting.
#   * A parallel hypothesis (materializing each chunk as a real TABLE instead
#     of a view, so the union only ever sees concrete already-resolved
#     schemas) was tested and confirmed to make the UNION step instant
#     (0.003s over 3 materialized tables holding ~21M real rows) — but
#     materializing means physically copying every row into the duckdb file,
#     and real per-chunk materialize time (60-164s @ 5,000 files each,
#     growing with real row volume) would make a full bootstrap dramatically
#     SLOWER overall, and permanently duplicates the entire warehouse inside
#     catalog.duckdb (defeating the "thin view over the parquet" design
#     intent stated at the top of this file). Rejected.
#   * The actual fix: `union_by_name=true`'s per-file auto-detection is what's
#     expensive to re-resolve on every reference — replacing it with an
#     EXPLICIT `schema=` map (a DuckDB read_parquet parameter that declares
#     every column's name/type up front, so no per-file schema probing or
#     reconciliation happens at all) measured 100,000 real files in 3.88s
#     (vs. the 20-30+ min union path) and, critically, is NOT a correctness
#     downgrade: tested directly against the two real files representing both
#     sides of the verified open_interest double/int64 drift (a real int64
#     file listed first, a real double file listed second, AND the reverse
#     order) — with the explicit schema forcing `open_interest` to DOUBLE (the
#     wider of the two, so every conversion is a safe widen, never a lossy
#     narrow), the resolved type and actual row values were identical and
#     correct in both orderings, unlike the (rejected) alternative of just
#     dropping union_by_name with no replacement, which silently resolves the
#     whole scan's schema from whichever file happens to be FIRST in the list
#     — file-order-dependent and capable of silently choosing the narrowing
#     (lossy) direction. See `_canonical_schema()` / `_SCHEMA_TYPE_OVERRIDES`
#     below. With every chunk view now built off the SAME explicit schema, all
#     chunk views share an identical, concrete column list — so the top-level
#     union no longer needs `BY NAME` reconciliation either; plain positional
#     `UNION ALL` over already-resolved schemas was confirmed to scale
#     linearly (0.035s/0.073s/0.147s/0.289s @ 2/4/8/16 chunks, ~112M rows in
#     the final count) instead of quadratically.

CATALOG_CHUNK_SIZE = 5000   # files per chunk view; bounds every rebuild's CREATE VIEW cost

# Arrow dtype name -> DuckDB SQL type, used to derive an explicit read_parquet
# schema= (see bug #4 above). Extend if the warehouse ever gains a genuinely
# new arrow dtype; falls back to VARCHAR (the safest/widest catch-all) for
# anything unrecognized rather than raising.
_ARROW_TO_DUCKDB = {
    "string": "VARCHAR", "large_string": "VARCHAR",
    "double": "DOUBLE", "float": "DOUBLE",
    "int64": "BIGINT", "int32": "BIGINT", "int16": "BIGINT", "int8": "BIGINT",
    "bool": "BOOLEAN",
}

# Real, verified schema-type drift (see bug #4 above): open_interest is
# `double` in some warehouse files and `int64` in others. Whichever real file
# happens to be used to derive the canonical schema (see _canonical_schema()),
# force this column to its WIDER type so every per-file conversion is a safe
# widen, never a lossy narrow — this is what makes the explicit-schema
# approach a correctness-preserving replacement for union_by_name, not a
# downgrade.
_SCHEMA_TYPE_OVERRIDES = {"open_interest": "DOUBLE"}


def _load_manifest_table(con) -> dict[str, dict]:
    rows = con.execute(
        "SELECT path, mtime, nonempty, chunk_id FROM _catalog_manifest"
    ).fetchall()
    return {r[0]: {"mtime": r[1], "nonempty": r[2], "chunk_id": r[3]} for r in rows}


def _ensure_catalog_tables(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS _catalog_manifest (
            path VARCHAR PRIMARY KEY,
            mtime DOUBLE NOT NULL,
            nonempty BOOLEAN NOT NULL,
            chunk_id INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS _catalog_chunks (
            chunk_id INTEGER PRIMARY KEY,
            file_count INTEGER NOT NULL
        )
    """)


def _canonical_schema(con) -> dict[str, str]:
    """Derive the {column_name: duckdb_type} schema used for every chunk's
    explicit read_parquet(schema=...) (see bug #4 in the module comment above).

    Derived from ONE representative already-classified non-empty file rather
    than hardcoded, so the same code path works for both the real ~41-column
    warehouse and the small synthetic fixtures in test_storage.py. The known
    real type-drift column (open_interest) is forced to its wider type
    regardless of what that one representative file happens to have — see
    _SCHEMA_TYPE_OVERRIDES. Returns {} if there is no non-empty file yet
    (empty warehouse) — callers must not need a schema in that case.
    """
    import pyarrow.parquet as pq

    row = con.execute(
        "SELECT path FROM _catalog_manifest WHERE nonempty = true ORDER BY path LIMIT 1"
    ).fetchone()
    if row is None:
        return {}
    arrow_schema = pq.read_metadata(row[0]).schema.to_arrow_schema()
    schema: dict[str, str] = {}
    for field in arrow_schema:
        duckdb_type = _ARROW_TO_DUCKDB.get(str(field.type), "VARCHAR")
        schema[field.name] = _SCHEMA_TYPE_OVERRIDES.get(field.name, duckdb_type)
    return schema


def _schema_map_sql(schema: dict[str, str]) -> str:
    """DuckDB MAP-of-STRUCT literal for read_parquet(schema=...) — each entry
    needs the {'name', 'type', 'default_value'} shape DuckDB 1.5.4 requires."""
    entries = ",".join(
        f"'{c}':{{'name':'{c}','type':'{t}','default_value':NULL}}"
        for c, t in schema.items()
    )
    return "MAP {" + entries + "}"


def _build_chunk_view(con, chunk_id: int, schema: dict[str, str]) -> None:
    """(Re)build one chunk's DuckDB view from its current manifest membership.

    Bounded cost: a chunk holds at most CATALOG_CHUNK_SIZE files by construction.
    Uses an explicit schema= (see bug #4 above) instead of union_by_name=true —
    measured ~100x+ faster at scale and, with the known drift column forced to
    its wider type, equally correct (verified against real drift-representative
    files in both orderings).
    """
    paths = [r[0] for r in con.execute(
        "SELECT path FROM _catalog_manifest WHERE chunk_id = ? AND nonempty = true "
        "ORDER BY path", [chunk_id]).fetchall()]
    view = f"_eod_chunk_{chunk_id}"
    if not paths:
        con.execute(f"DROP VIEW IF EXISTS {view}")
        return
    lit = "[" + ",".join("'" + p.replace("'", "''") + "'" for p in paths) + "]"
    con.execute(f"CREATE OR REPLACE VIEW {view} AS "
                f"SELECT * FROM read_parquet({lit}, filename=true, "
                f"schema={_schema_map_sql(schema)})")


def _rebuild_union_view(con) -> None:
    """Cheap: text only lists chunk-view names, never raw file paths. Uses
    plain positional UNION ALL, not UNION ALL BY NAME — every chunk view is
    now built off the same explicit canonical schema (see bug #4 above), so
    there is no need (and, per measurement, real cost) to reconcile by name."""
    chunk_ids = [r[0] for r in con.execute(
        "SELECT chunk_id FROM _catalog_chunks WHERE file_count > 0 "
        "ORDER BY chunk_id").fetchall()]
    if not chunk_ids:
        con.execute("CREATE OR REPLACE VIEW options_eod AS SELECT NULL WHERE false")
        return
    parts = [f"SELECT * FROM _eod_chunk_{cid}" for cid in chunk_ids]
    con.execute("CREATE OR REPLACE VIEW options_eod AS " + " UNION ALL ".join(parts))


def rebuild_catalog() -> None:
    """Incrementally (re)build the DuckDB `options_eod` view over the warehouse.

    See the module-level "Incremental catalog rebuild" comment above for the full
    design rationale. Summary: only files that are new or whose mtime changed
    since the last call are (re)classified (pyarrow metadata read); the rest is a
    cheap os.stat scan. Only the chunk(s) receiving membership changes are
    rebuilt; sealed chunks are never re-touched. Safe to call as often as desired
    — a no-op call (nothing changed on disk) does a full-tree stat scan (seconds)
    and touches no views at all.
    """
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq
    from concurrent.futures import ThreadPoolExecutor

    con = duckdb.connect(str(config.CATALOG_DB))
    try:
        _ensure_catalog_tables(con)

        # Cheap: path + mtime only, no parquet parsing. Fast even at 300k+ files
        # (measured 7s @ 311,988 real files, 2026-07-10).
        on_disk: dict[str, float] = {}
        for f in config.RAW_OPTIONS.glob("*/*.parquet"):
            on_disk[str(f).replace("\\", "/")] = f.stat().st_mtime

        manifest = _load_manifest_table(con)
        chunk_counts = {r[0]: r[1] for r in con.execute(
            "SELECT chunk_id, file_count FROM _catalog_chunks").fetchall()}

        dirty = [p for p, mt in on_disk.items()
                 if p not in manifest or manifest[p]["mtime"] != mt]

        # Files that disappeared from disk since the last rebuild (should not
        # happen in this warehouse's normal write-once-then-atomic-replace model,
        # but guard anyway rather than leave a stale path in a chunk forever).
        vanished = [p for p in manifest if p not in on_disk]

        newly_included: list[str] = []      # need chunk assignment
        chunks_to_rebuild: set[int] = set()
        manifest_deletes: list[str] = []

        def _release(chunk_id: int | None) -> None:
            """A file is leaving chunk_id's membership (flip to empty, or
            vanished from disk) — shrink its count and mark it for rebuild."""
            if chunk_id is None:
                return
            chunk_counts[chunk_id] = max(0, chunk_counts.get(chunk_id, 0) - 1)
            chunks_to_rebuild.add(chunk_id)

        # BACKGROUND (2026-07-10, bug #3, found running the real 311,988-file
        # bootstrap after fixing bugs #1/#2 above): this classify step -- one
        # pq.read_metadata() call per dirty file -- is I/O-bound (opening each
        # file and reading its footer) and was a plain sequential Python loop.
        # Measured on a real, random, diverse 4,000-file sample of the actual
        # warehouse: 0.82ms/file single-threaded (~256s / 4.3min projected for
        # the full 311,988-file bootstrap) vs a consistent ~3.7-3.9x speedup
        # from a ThreadPoolExecutor at 8-16 workers (pyarrow's C++ parquet
        # footer read releases the GIL, so threads -- not processes -- are
        # enough here; no new dependency, concurrent.futures is stdlib).
        # Diminishing returns past ~16 workers on this machine. This runs
        # BEFORE the loop below so the loop itself stays a simple, easy-to-
        # read sequential pass over pre-computed results -- only the I/O is
        # parallelized, the bookkeeping (chunk assignment order, manifest
        # updates) stays single-threaded and deterministic.
        def _classify_one(p: str) -> bool:
            try:
                return pq.read_metadata(p).num_columns > 0
            except Exception:
                # Unreadable/corrupt footer -> treat as not-includable, matching
                # the old _nonempty_parquets() behavior.
                return False

        CLASSIFY_WORKERS = 8
        with ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as ex:
            classifications = dict(zip(dirty, ex.map(_classify_one, dirty)))

        for p in dirty:
            nonempty = classifications[p]
            prev = manifest.get(p)
            was_included = bool(prev and prev["nonempty"])
            chunk_id = prev["chunk_id"] if prev else None
            if nonempty and not was_included:
                newly_included.append(p)
                chunk_id = None   # assigned below
            elif not nonempty and was_included:
                # flip nonempty -> empty: must be pulled out of its chunk.
                _release(chunk_id)
                chunk_id = None
            manifest[p] = {"mtime": on_disk[p], "nonempty": nonempty, "chunk_id": chunk_id}

        for p in vanished:
            prev = manifest.pop(p)
            if prev["nonempty"]:
                _release(prev["chunk_id"])
            manifest_deletes.append(p)

        # Assign newly-included files to chunks, filling the currently-open chunk
        # (file_count < CATALOG_CHUNK_SIZE) before opening a new one. Reuses any
        # chunk_id left under-capacity by a removal above, not just the max id.
        # Cached as a simple cursor rather than re-scanning chunk_counts per file —
        # matters at bootstrap scale (hundreds of thousands of newly_included files
        # in one call against a fresh manifest).
        candidates = sorted(cid for cid, cnt in chunk_counts.items()
                            if cnt < CATALOG_CHUNK_SIZE)
        cursor = iter(candidates)
        open_chunk = next(cursor, None)
        next_new_id = (max(chunk_counts) + 1) if chunk_counts else 0

        for p in newly_included:
            if open_chunk is None:
                open_chunk = next_new_id
                next_new_id += 1
                chunk_counts[open_chunk] = 0
            manifest[p]["chunk_id"] = open_chunk
            chunk_counts[open_chunk] += 1
            chunks_to_rebuild.add(open_chunk)
            if chunk_counts[open_chunk] >= CATALOG_CHUNK_SIZE:
                open_chunk = next(cursor, None)

        final_upserts = [(p, manifest[p]["mtime"], manifest[p]["nonempty"],
                          manifest[p]["chunk_id"]) for p in dirty]

        # BACKGROUND (2026-07-10, bug #2): the original code here used
        # con.executemany(...) to write these tables one row at a time. On a
        # bootstrap run (empty manifest, every one of the ~312k files "dirty")
        # that is ~312k individual parameterized statements — a classic DuckDB
        # slow path. A real production run was killed after 46+ minutes with no
        # completion. Fix: build each batch as a pyarrow Table (nulls in
        # chunk_id are first-class in pyarrow regardless of int type, unlike
        # pandas which would silently upcast a column with mixed int/None to
        # float64), register it as a queryable relation, and do ONE set-based
        # DELETE + ONE set-based INSERT per batch instead of one round-trip per
        # row. Upsert semantics (a path already in the manifest gets its row
        # overwritten, not duplicated) are preserved explicitly: bulk DELETE any
        # path present in the incoming batch, immediately followed by a bulk
        # INSERT of the whole batch — both still inside the same transaction as
        # before, so the write stays atomic (a kill mid-way rolls back cleanly).
        con.execute("BEGIN TRANSACTION")
        if manifest_deletes:
            deletes_tbl = pa.table({"path": pa.array(manifest_deletes, type=pa.string())})
            con.register("_deletes_tbl", deletes_tbl)
            con.execute("DELETE FROM _catalog_manifest WHERE path IN "
                        "(SELECT path FROM _deletes_tbl)")
            con.unregister("_deletes_tbl")
        if final_upserts:
            paths, mtimes, nonemptys, chunk_ids = zip(*final_upserts)
            upserts_tbl = pa.table({
                "path": pa.array(paths, type=pa.string()),
                "mtime": pa.array(mtimes, type=pa.float64()),
                "nonempty": pa.array(nonemptys, type=pa.bool_()),
                "chunk_id": pa.array(chunk_ids, type=pa.int32()),
            })
            con.register("_upserts_tbl", upserts_tbl)
            con.execute("DELETE FROM _catalog_manifest WHERE path IN "
                        "(SELECT path FROM _upserts_tbl)")
            con.execute("INSERT INTO _catalog_manifest SELECT * FROM _upserts_tbl")
            con.unregister("_upserts_tbl")
        con.execute("DELETE FROM _catalog_chunks")
        if chunk_counts:
            ids, counts = zip(*chunk_counts.items())
            chunks_tbl = pa.table({
                "chunk_id": pa.array(ids, type=pa.int32()),
                "file_count": pa.array(counts, type=pa.int32()),
            })
            con.register("_chunks_tbl", chunks_tbl)
            con.execute("INSERT INTO _catalog_chunks SELECT * FROM _chunks_tbl")
            con.unregister("_chunks_tbl")
        con.execute("COMMIT")

        # Robustness (found empirically 2026-07-10 running the real bootstrap):
        # a kill landing AFTER this commit but BEFORE the per-chunk view loop
        # below finishes leaves _catalog_manifest/_catalog_chunks fully populated
        # and correct, but one or more _eod_chunk_<n> views never built. Because
        # chunks_to_rebuild above is derived purely from files that were dirty
        # THIS call, a later no-op call (nothing changed on disk -> nothing
        # dirty) would never notice or repair the gap on its own. Guard by also
        # rebuilding any chunk that _catalog_chunks says should exist (non-zero
        # file_count) but that has no live DuckDB view yet -- cheap (metadata-
        # only query), and a true no-op call still costs one extra fast lookup.
        existing_views = {r[0] for r in con.execute(
            "SELECT view_name FROM duckdb_views() WHERE view_name LIKE '\\_eod\\_chunk\\_%' "
            "ESCAPE '\\'").fetchall()}
        for cid, cnt in chunk_counts.items():
            if cnt > 0 and f"_eod_chunk_{cid}" not in existing_views:
                chunks_to_rebuild.add(cid)

        if chunks_to_rebuild:
            schema = _canonical_schema(con)
            for cid in sorted(chunks_to_rebuild):
                _build_chunk_view(con, cid, schema)

        _rebuild_union_view(con)
    finally:
        con.close()
