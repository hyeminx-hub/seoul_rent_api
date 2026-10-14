import argparse
import csv
import gzip
import io
import json
import re
import sys
from contextlib import ExitStack
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

if __package__:
    from scripts.audit_data import decode_csv
else:
    from audit_data import decode_csv

API_FIELDS = (
    "RCPT_YR",
    "CGG_CD",
    "CGG_NM",
    "STDG_CD",
    "STDG_NM",
    "LOTNO_SE",
    "LOTNO_SE_NM",
    "MNO",
    "SNO",
    "FLR",
    "CTRT_DAY",
    "RENT_SE",
    "RENT_AREA",
    "GRFE",
    "RTFE",
    "BLDG_NM",
    "ARCH_YR",
    "BLDG_USG",
    "CTRT_PRD",
    "NEW_UPDT_YN",
    "CTRT_UPDT_USE_YN",
    "BFR_GRFE",
    "BFR_RTFE",
)
CSV_TO_API = {
    "접수년도": "RCPT_YR",
    "자치구코드": "CGG_CD",
    "자치구명": "CGG_NM",
    "법정동코드": "STDG_CD",
    "법정동명": "STDG_NM",
    "지번구분코드": "LOTNO_SE",
    "지번구분": "LOTNO_SE_NM",
    "본번": "MNO",
    "부번": "SNO",
    "층": "FLR",
    "계약일": "CTRT_DAY",
    "전월세구분": "RENT_SE",
    "임대면적": "RENT_AREA",
    "보증금(만원)": "GRFE",
    "임대료(만원)": "RTFE",
    "건물명": "BLDG_NM",
    "건축년도": "ARCH_YR",
    "건물용도": "BLDG_USG",
    "계약기간": "CTRT_PRD",
    "신규계약구분": "NEW_UPDT_YN",
    "갱신청구권사용": "CTRT_UPDT_USE_YN",
    "종전보증금": "BFR_GRFE",
    "종전임대료": "BFR_RTFE",
}
NUMERIC_FIELDS = {
    "FLR",
    "RENT_AREA",
    "GRFE",
    "RTFE",
    "ARCH_YR",
    "BFR_GRFE",
    "BFR_RTFE",
}
OUTPUT_FIELDS = (*API_FIELDS, "SOURCE_FILE", "SOURCE_ROW")
SUPPORTED_SUFFIXES = {".csv", ".txt", ".jsonl", ".ndjson"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize all Seoul rental source files to one gzipped CSV."
    )
    parser.add_argument("--input-dir", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/seoul_rent_standardized.csv.gz"),
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{8}", text):
        normalized = text
    else:
        match = re.fullmatch(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D*", text)
        if match is None:
            raise ValueError(f"Invalid contract date: {text!r}")
        year, month, day = match.groups()
        normalized = f"{year}{month.zfill(2)}{day.zfill(2)}"
    try:
        return datetime.strptime(normalized, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"Invalid contract date: {text!r}") from exc


def normalize_numeric(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric value: {text!r}") from exc
    if not number.is_finite():
        raise ValueError(f"Invalid numeric value: {text!r}")
    return format(number.normalize(), "f")


def normalize_row(
    source_row: dict[str, object],
    source_file: str,
    source_row_number: int,
    *,
    is_api: bool,
) -> dict[str, str]:
    if is_api:
        row = source_row
        missing = set(API_FIELDS).difference(row)
        if missing:
            raise ValueError(
                f"API row is missing expected fields: {', '.join(sorted(missing))}"
            )
    else:
        row = {
            API_FIELD: source_row.get(CSV_FIELD, "")
            for CSV_FIELD, API_FIELD in CSV_TO_API.items()
        }

    normalized: dict[str, str] = {}
    for field in API_FIELDS:
        value = row.get(field, "")
        if field == "CTRT_DAY":
            normalized[field] = normalize_date(value)
        elif field in NUMERIC_FIELDS:
            normalized[field] = normalize_numeric(value)
        else:
            normalized[field] = "" if value is None else str(value).strip()
    normalized["SOURCE_FILE"] = source_file
    normalized["SOURCE_ROW"] = str(source_row_number)
    return normalized


def iter_rows(path: Path):
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid JSON.") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"{path}:{line_number}: expected a JSON object.")
                yield line_number, row, True
        return

    stream, _encoding, delimiter = decode_csv(path)
    with stream:
        reader = csv.DictReader(stream, delimiter=delimiter)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: missing CSV header.")
        missing = set(CSV_TO_API).difference(name.strip() for name in reader.fieldnames)
        if missing:
            raise ValueError(f"{path}: missing expected columns: {', '.join(sorted(missing))}")
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"{path}:{row_number}: unexpected extra CSV values.")
            cleaned = {
                key.strip(): value for key, value in row.items() if key is not None
            }
            yield row_number, cleaned, False


def normalize(input_dir: Path, output: Path) -> int:
    paths = sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not paths:
        raise ValueError(f"No supported source files found in {input_dir}.")
    if output.resolve().is_relative_to(input_dir.resolve()):
        raise ValueError("Output must be outside the raw input directory.")

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    if output.exists():
        raise FileExistsError(f"Output already exists; refusing to overwrite: {output}")
    if partial.exists():
        raise FileExistsError(
            f"Partial output already exists; inspect it before retrying: {partial}"
        )

    row_count = 0
    source_counts: dict[str, int] = {}
    try:
        with ExitStack() as stack:
            raw_output = stack.enter_context(partial.open("xb"))
            compressed = gzip.GzipFile(
                fileobj=raw_output,
                mode="wb",
                filename="",
                mtime=0,
            )
            text_output: TextIO = stack.enter_context(
                io.TextIOWrapper(compressed, encoding="utf-8", newline="")
            )
            writer = csv.DictWriter(
                text_output,
                fieldnames=OUTPUT_FIELDS,
                extrasaction="raise",
                lineterminator="\n",
            )
            writer.writeheader()

            for path in paths:
                source_count = 0
                for row_number, source_row, is_api in iter_rows(path):
                    try:
                        normalized_row = normalize_row(
                            source_row,
                            path.name,
                            row_number,
                            is_api=is_api,
                        )
                    except ValueError as exc:
                        raise ValueError(f"{path}:{row_number}: {exc}") from exc
                    writer.writerow(normalized_row)
                    source_count += 1
                    row_count += 1
                source_counts[path.name] = source_count
                print(
                    f"Normalized {path.name}: {source_count} row(s).",
                    flush=True,
                )
    except Exception:
        print(f"Normalization failed; partial output retained at {partial}.", file=sys.stderr)
        raise

    partial.replace(output)
    print(
        f"Normalization complete: {row_count} row(s) across "
        f"{len(source_counts)} source file(s). Output: {output}"
    )
    return 0


def main() -> int:
    args = parse_args()
    try:
        return normalize(args.input_dir, args.output)
    except (FileExistsError, OSError, UnicodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
