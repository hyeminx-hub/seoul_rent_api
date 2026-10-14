import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TextIO

TARGET_YEARS = range(2021, 2026)
DATE_FIELDS = ("계약일", "CTRT_DAY")
RENT_FIELDS = ("전월세구분", "RENT_SE")
AMOUNT_FIELDS = ("보증금(만원)", "임대료(만원)", "GRFE", "RTFE")
DELIMITERS = ",\t;|"


def find_field(row: dict[str, object], names: tuple[str, ...]) -> object:
    for name in names:
        if name in row:
            return row[name]
    return None


def contract_year(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"\d{8}", text):
        normalized = text
    else:
        match = re.fullmatch(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D*", text)
        if match is None:
            return None
        year, month, day = match.groups()
        normalized = f"{year}{month.zfill(2)}{day.zfill(2)}"
    try:
        return datetime.strptime(normalized, "%Y%m%d").year
    except ValueError:
        return None


def is_valid_amount(value: object) -> bool:
    if value is None or not str(value).strip():
        return True
    normalized = str(value).strip().replace(",", "")
    try:
        float(normalized)
    except ValueError:
        return False
    return True


def decode_csv(path: Path) -> tuple[TextIO, str, str]:
    for encoding in ("utf-8-sig", "cp949"):
        stream = path.open("r", encoding=encoding, newline="")
        try:
            sample = stream.read(8192)
        except UnicodeDecodeError:
            stream.close()
            continue
        stream.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=DELIMITERS)
        except csv.Error:
            dialect = csv.excel
        try:
            next(csv.reader([sample.splitlines()[0]], dialect), [])
        except (IndexError, UnicodeDecodeError, csv.Error):
            stream.close()
            continue
        return stream, encoding, dialect.delimiter
    raise ValueError(f"{path}: unsupported text encoding.")


def audit_csv(path: Path) -> tuple[Counter[int], int, int, int, set[str], str]:
    years: Counter[int] = Counter()
    invalid_dates = 0
    invalid_amounts = 0
    row_count = 0
    rent_types: set[str] = set()
    stream, encoding, delimiter = decode_csv(path)
    with stream:
        reader = csv.DictReader(stream, delimiter=delimiter)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: missing CSV header.")
        fieldnames = {name.strip() for name in reader.fieldnames if name}
        if not any(name in fieldnames for name in DATE_FIELDS):
            raise ValueError(f"{path}: missing contract-date column.")
        if not any(name in fieldnames for name in RENT_FIELDS):
            raise ValueError(f"{path}: missing rent-type column.")
        if not any(name in fieldnames for name in AMOUNT_FIELDS):
            raise ValueError(f"{path}: missing deposit/rent columns.")

        for row in reader:
            row_count += 1
            normalized_row = {
                (key.strip() if key else key): value for key, value in row.items()
            }
            year = contract_year(find_field(normalized_row, DATE_FIELDS))
            if year is None:
                invalid_dates += 1
            else:
                years[year] += 1
            rent_type = find_field(normalized_row, RENT_FIELDS)
            if rent_type is not None and str(rent_type).strip():
                rent_types.add(str(rent_type).strip())
            if any(
                not is_valid_amount(normalized_row.get(field))
                for field in AMOUNT_FIELDS
                if field in normalized_row
            ):
                invalid_amounts += 1
    return years, row_count, invalid_dates, invalid_amounts, rent_types, encoding


def audit_jsonl(path: Path) -> tuple[Counter[int], int, int, int, set[str], str]:
    years: Counter[int] = Counter()
    invalid_dates = 0
    invalid_amounts = 0
    row_count = 0
    rent_types: set[str] = set()
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
            row_count += 1
            year = contract_year(find_field(row, DATE_FIELDS))
            if year is None:
                invalid_dates += 1
            else:
                years[year] += 1
            rent_type = find_field(row, RENT_FIELDS)
            if rent_type is not None and str(rent_type).strip():
                rent_types.add(str(rent_type).strip())
            if any(
                not is_valid_amount(row.get(field))
                for field in AMOUNT_FIELDS
                if field in row
            ):
                invalid_amounts += 1
    return years, row_count, invalid_dates, invalid_amounts, rent_types, "utf-8"


def main() -> int:
    raw_dir = Path("data/raw")
    paths = sorted(
        path
        for path in raw_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".csv", ".txt", ".jsonl", ".ndjson"}
    ) if raw_dir.exists() else []
    if not paths:
        print("No supported source files found in data/raw/.", file=sys.stderr)
        return 1

    total_years: Counter[int] = Counter()
    invalid_dates_total = 0
    invalid_amounts_total = 0
    try:
        for path in paths:
            if path.suffix.lower() in {".jsonl", ".ndjson"}:
                result = audit_jsonl(path)
            else:
                result = audit_csv(path)
            years, row_count, invalid_dates, invalid_amounts, rent_types, encoding = result
            total_years.update(years)
            invalid_dates_total += invalid_dates
            invalid_amounts_total += invalid_amounts
            covered_years = sorted(year for year in years if year in TARGET_YEARS)
            year_summary = ", ".join(
                f"{year}:{years[year]}" for year in covered_years
            ) or "none"
            print(
                f"{path.name}: rows={row_count}, encoding={encoding}, "
                f"target-year rows=[{year_summary}], invalid_dates={invalid_dates}, "
                f"rows_with_invalid_amounts={invalid_amounts}, "
                f"rent_types={','.join(sorted(rent_types)) or 'none'}"
            )
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Data audit failed: {exc}", file=sys.stderr)
        return 1

    missing_years = [year for year in TARGET_YEARS if total_years[year] == 0]
    print(
        "Target-year coverage (row totals across files; duplicates not removed): "
        + ", ".join(f"{year}={total_years[year]}" for year in TARGET_YEARS)
    )
    print(
        f"Invalid dates: {invalid_dates_total}; "
        f"rows with invalid amounts: {invalid_amounts_total}"
    )
    if missing_years or invalid_dates_total or invalid_amounts_total:
        print(
            f"Not ready for five-year analysis; missing years: "
            f"{', '.join(map(str, missing_years)) or 'none'}."
        )
        return 2

    print(
        "All target years and basic field checks passed. "
        "Duplicates and source completeness still require separate validation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
