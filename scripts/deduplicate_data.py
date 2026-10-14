import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import ExitStack, closing
from itertools import groupby
from pathlib import Path
from typing import TextIO

if __package__:
    from scripts.normalize_data import API_FIELDS, OUTPUT_FIELDS
else:
    from normalize_data import API_FIELDS, OUTPUT_FIELDS

IDENTITY_FIELDS = API_FIELDS
DEFAULT_INPUT = Path("data/processed/seoul_rent_standardized.csv.gz")
DEFAULT_OUTPUT = Path("data/processed/seoul_rent_deduplicated.csv.gz")
DEFAULT_REMOVED = Path("data/processed/seoul_rent_overlap_removed.csv.gz")
DEFAULT_AUDIT = Path("data/processed/seoul_rent_overlap_groups.csv.gz")
AUDIT_FIELDS = (
    "IDENTITY_SHA256",
    "SOURCE_FILES",
    "SOURCE_COUNTS",
    "RECEIPT_YEARS",
    "SELECTED_SOURCE",
    "RETAINED_ROWS",
    "REMOVED_ROWS",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove cross-source duplicate rows while preserving source-level repeats."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--removed", type=Path, default=DEFAULT_REMOVED)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    return parser.parse_args()


def open_csv(path: Path, mode: str) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, mode, encoding="utf-8", newline="")
    return path.open(mode, encoding="utf-8", newline="")


def identity_hash(row: dict[str, str]) -> bytes:
    identity = json.dumps(
        [row[field] for field in IDENTITY_FIELDS],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).digest()


def ensure_distinct_paths(paths: tuple[Path, ...]) -> None:
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("Input and output paths must all be different.")


def _add_batch(
    database: sqlite3.Connection,
    batch: list[tuple[bytes, str, str, int]],
) -> None:
    database.executemany(
        """
        INSERT INTO source_counts (identity_hash, source_file, receipt_year, row_count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (identity_hash, source_file, receipt_year)
        DO UPDATE SET row_count = row_count + excluded.row_count
        """,
        batch,
    )


def _audit_row(
    identity: bytes,
    counts_by_source: dict[str, int],
    receipt_years: set[str],
    selected_source: str,
    selected_count: int,
) -> dict[str, object]:
    return {
        "IDENTITY_SHA256": identity.hex(),
        "SOURCE_FILES": json.dumps(sorted(counts_by_source), ensure_ascii=False),
        "SOURCE_COUNTS": json.dumps(counts_by_source, ensure_ascii=False, sort_keys=True),
        "RECEIPT_YEARS": json.dumps(sorted(receipt_years)),
        "SELECTED_SOURCE": selected_source,
        "RETAINED_ROWS": selected_count,
        "REMOVED_ROWS": sum(counts_by_source.values()) - selected_count,
    }


def _managed_gzip_writer(
    stack: ExitStack,
    path: Path,
    fields: tuple[str, ...],
) -> csv.DictWriter:
    raw_file = stack.enter_context(path.open("xb"))
    compressed = gzip.GzipFile(
        fileobj=raw_file,
        mode="wb",
        filename="",
        mtime=0,
    )
    text = stack.enter_context(io.TextIOWrapper(compressed, encoding="utf-8", newline=""))
    writer = csv.DictWriter(
        text,
        fieldnames=fields,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    return writer


def deduplicate(
    input_path: Path,
    output_path: Path,
    removed_path: Path,
    audit_path: Path,
) -> dict[str, int]:
    paths = (input_path, output_path, removed_path, audit_path)
    ensure_distinct_paths(paths)
    if not input_path.is_file():
        raise FileNotFoundError(f"Standardized input not found: {input_path}")
    for path in (output_path, removed_path, audit_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"Output already exists; refusing to overwrite: {path}")
        partial = path.with_suffix(path.suffix + ".part")
        if partial.exists():
            raise FileExistsError(
                f"Partial output already exists; inspect it before retrying: {partial}"
            )

    descriptor, database_name = tempfile.mkstemp(
        prefix=".seoul-rent-dedupe-",
        suffix=".sqlite3",
        dir=output_path.parent,
    )
    os.close(descriptor)
    database_path = Path(database_name)
    output_part, removed_part, audit_part = (
        path.with_suffix(path.suffix + ".part")
        for path in (output_path, removed_path, audit_path)
    )

    try:
        with closing(sqlite3.connect(database_path)) as database, database:
            database.execute("PRAGMA journal_mode=OFF")
            database.execute("PRAGMA synchronous=OFF")
            database.execute(
                """
                CREATE TABLE source_counts (
                    identity_hash BLOB NOT NULL,
                    source_file TEXT NOT NULL,
                    receipt_year TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    PRIMARY KEY (identity_hash, source_file, receipt_year)
                ) WITHOUT ROWID
                """
            )
            source_order: dict[str, int] = {}
            input_rows = 0
            batch: list[tuple[bytes, str, str, int]] = []
            with open_csv(input_path, "rt") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != list(OUTPUT_FIELDS):
                    raise ValueError(
                        f"Unexpected standardized columns in {input_path}; "
                        "rebuild the standardized file before deduplicating."
                    )
                for row in reader:
                    source = row["SOURCE_FILE"]
                    if source not in source_order:
                        source_order[source] = len(source_order)
                    batch.append(
                        (identity_hash(row), source, row["RCPT_YR"], 1)
                    )
                    input_rows += 1
                    if len(batch) >= 10_000:
                        _add_batch(database, batch)
                        batch.clear()
                    if input_rows % 500_000 == 0:
                        print(f"Indexed {input_rows:,} rows.", flush=True)
            if batch:
                _add_batch(database, batch)
            database.commit()

            allocations: dict[bytes, tuple[str, int]] = {}
            overlap_groups = 0
            removed_rows = 0
            intra_source_repeat_groups = 0
            intra_source_repeat_rows = 0
            for identity, entries_iter in groupby(
                database.execute(
                    """
                    SELECT identity_hash, source_file, receipt_year, row_count
                    FROM source_counts
                    ORDER BY identity_hash, source_file, receipt_year
                    """
                ),
                key=lambda item: item[0],
            ):
                entries = list(entries_iter)
                counts_by_source: dict[str, int] = {}
                receipt_years: set[str] = set()
                for _identity, source, receipt_year, count in entries:
                    counts_by_source[source] = counts_by_source.get(source, 0) + count
                    if receipt_year:
                        receipt_years.add(receipt_year)
                intra_source_repeat_groups += sum(
                    count > 1 for count in counts_by_source.values()
                )
                intra_source_repeat_rows += sum(
                    count - 1 for count in counts_by_source.values() if count > 1
                )
                if len(counts_by_source) < 2:
                    continue

                overlap_groups += 1
                selected_source = min(
                    counts_by_source,
                    key=lambda source: (
                        -counts_by_source[source],
                        source_order[source],
                    ),
                )
                selected_count = counts_by_source[selected_source]
                allocations[identity] = (selected_source, selected_count)
                removed_rows += sum(counts_by_source.values()) - selected_count

            output_rows = 0
            actual_removed_rows = 0
            with ExitStack() as stack:
                output_writer = _managed_gzip_writer(stack, output_part, OUTPUT_FIELDS)
                removed_writer = _managed_gzip_writer(stack, removed_part, OUTPUT_FIELDS)
                audit_writer = _managed_gzip_writer(stack, audit_part, AUDIT_FIELDS)

                for identity, entries_iter in groupby(
                    database.execute(
                        """
                        SELECT identity_hash, source_file, receipt_year, row_count
                        FROM source_counts
                        ORDER BY identity_hash, source_file, receipt_year
                        """
                    ),
                    key=lambda item: item[0],
                ):
                    entries = list(entries_iter)
                    counts_by_source: dict[str, int] = {}
                    receipt_years: set[str] = set()
                    for _identity, source, receipt_year, count in entries:
                        counts_by_source[source] = counts_by_source.get(source, 0) + count
                        if receipt_year:
                            receipt_years.add(receipt_year)
                    if len(counts_by_source) < 2:
                        continue
                    selected_source, selected_count = allocations[identity]
                    audit_writer.writerow(
                        _audit_row(
                            identity,
                            counts_by_source,
                            receipt_years,
                            selected_source,
                            selected_count,
                        )
                    )

                with open_csv(input_path, "rt") as stream:
                    for row in csv.DictReader(stream):
                        allocation = allocations.get(identity_hash(row))
                        if allocation is None or row["SOURCE_FILE"] == allocation[0]:
                            output_writer.writerow(row)
                            output_rows += 1
                        else:
                            removed_writer.writerow(row)
                            actual_removed_rows += 1
                        if (output_rows + actual_removed_rows) % 500_000 == 0:
                            print(
                                f"Processed {output_rows + actual_removed_rows:,} rows.",
                                flush=True,
                            )

            if actual_removed_rows != removed_rows:
                raise RuntimeError(
                    f"Internal removal-count mismatch: expected {removed_rows}, "
                    f"wrote {actual_removed_rows}."
                )
            if output_rows + actual_removed_rows != input_rows:
                raise RuntimeError("Output row totals do not match the input row total.")

        for partial, final in zip(
            (output_part, removed_part, audit_part),
            (output_path, removed_path, audit_path),
        ):
            partial.replace(final)

        return {
            "input_rows": input_rows,
            "output_rows": output_rows,
            "overlap_groups": overlap_groups,
            "removed_rows": actual_removed_rows,
            "intra_source_repeat_groups_preserved": intra_source_repeat_groups,
            "intra_source_repeat_rows_preserved": intra_source_repeat_rows,
        }
    finally:
        database_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    try:
        summary = deduplicate(args.input, args.output, args.removed, args.audit)
    except (FileExistsError, FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Deduplication failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Clean dataset: {args.output}")
    print(f"Removed source-overlap rows: {args.removed}")
    print(f"Overlap audit: {args.audit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
