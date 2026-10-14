import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SERVICE = "tbLnOpendataRentV"
API_BASE_URL = "http://openapi.seoul.go.kr:8088"
MAX_PAGE_SIZE = 1000
DEFAULT_RECEIPT_YEARS = (2026, 2025, 2024)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch all Seoul rental records for selected receipt years."
    )
    parser.add_argument(
        "--receipt-years",
        type=int,
        nargs="+",
        choices=(2024, 2025, 2026),
        default=list(DEFAULT_RECEIPT_YEARS),
        help="Receipt years to fetch (default: 2026 2025 2024).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=MAX_PAGE_SIZE,
        help=f"Rows per page (1-{MAX_PAGE_SIZE}; default: {MAX_PAGE_SIZE}).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Optional page limit per receipt year; omit to fetch every page.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/raw"),
        help="Directory for per-year JSONL files (default: data/raw).",
    )
    args = parser.parse_args()

    if len(args.receipt_years) != len(set(args.receipt_years)):
        parser.error("--receipt-years must not contain duplicates")
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be at least 1")
    if not 1 <= args.page_size <= MAX_PAGE_SIZE:
        parser.error(f"--page-size must be between 1 and {MAX_PAGE_SIZE}")
    return args


def read_api_key() -> str:
    api_key = os.environ.get("SEOUL_OPEN_DATA_API_KEY", "").strip()
    if not api_key:
        raise ValueError("SEOUL_OPEN_DATA_API_KEY is not set.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", api_key):
        raise ValueError("SEOUL_OPEN_DATA_API_KEY has an invalid format.")
    return api_key


def parse_page(payload: object) -> tuple[int, list[dict[str, object]]]:
    if not isinstance(payload, dict):
        raise ValueError("API response has an unexpected top-level structure.")

    top_level_result = payload.get("RESULT")
    if isinstance(top_level_result, dict):
        code = top_level_result.get("CODE")
        if code == "INFO-200":
            return 0, []
        raise ValueError(
            f"API error {code or 'unknown'}: "
            f"{top_level_result.get('MESSAGE', 'No message returned')}"
        )

    result_set = payload.get(SERVICE)
    if not isinstance(result_set, dict):
        raise ValueError("API response does not contain the expected service object.")

    result = result_set.get("RESULT")
    if not isinstance(result, dict):
        raise ValueError("API response does not contain a valid result status.")
    if result.get("CODE") != "INFO-000":
        raise ValueError(
            f"API error {result.get('CODE', 'unknown')}: "
            f"{result.get('MESSAGE', 'No message returned')}"
        )

    total = result_set.get("list_total_count")
    rows = result_set.get("row")
    if not isinstance(total, int) or total < 0:
        raise ValueError("API response contains an invalid total record count.")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("API response does not contain a valid row list.")
    return total, rows


def fetch_page(api_key: str, start: int, end: int, receipt_year: int) -> object:
    url = f"{API_BASE_URL}/{api_key}/json/{SERVICE}/{start}/{end}/{receipt_year}/"
    request = Request(url, headers={"User-Agent": "seoul-rent-analysis/0.1"})
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"API returned HTTP {exc.code}.") from None
    except URLError as exc:
        raise RuntimeError(
            f"Could not reach the API ({type(exc.reason).__name__})."
        ) from None
    except TimeoutError:
        raise RuntimeError("API request timed out.") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("API returned invalid JSON.") from None


def collect_year(
    api_key: str,
    receipt_year: int,
    page_size: int,
    max_pages: int | None,
    output_dir: Path,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"api_receipt_{receipt_year}.jsonl"
    partial = output.with_suffix(output.suffix + ".part")
    if output.exists():
        raise FileExistsError(
            f"Output already exists; refusing to overwrite: {output}"
        )

    total_records: int | None = None
    pages_read = 0
    rows_written = 0
    next_index = 1

    with partial.open("w", encoding="utf-8") as output_file:
        while max_pages is None or pages_read < max_pages:
            end_index = next_index + page_size - 1
            payload = fetch_page(api_key, next_index, end_index, receipt_year)
            page_total, rows = parse_page(payload)

            if total_records is None:
                total_records = page_total
            elif page_total != total_records:
                raise RuntimeError(
                    "API record count changed during collection; "
                    f"partial file retained: {partial}"
                )

            if next_index > total_records:
                break
            expected_rows = min(page_size, total_records - next_index + 1)
            if len(rows) != expected_rows:
                raise RuntimeError(
                    "API returned an unexpected number of rows; "
                    f"partial file retained: {partial}"
                )

            for row in rows:
                if str(row.get("RCPT_YR", "")) != str(receipt_year):
                    raise RuntimeError(
                        f"API returned a row outside receipt year {receipt_year}; "
                        f"partial file retained: {partial}"
                    )
                output_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows_written += 1

            pages_read += 1
            next_index += page_size
            output_file.flush()

            if pages_read % 50 == 0:
                print(
                    f"Receipt year {receipt_year}: {pages_read} page(s), "
                    f"{rows_written} row(s) saved.",
                    flush=True,
                )

            if next_index > total_records:
                break

    if total_records is None:
        raise RuntimeError(
            f"No response returned for receipt year {receipt_year}; "
            f"partial file retained: {partial}"
        )

    complete = next_index > total_records
    if complete:
        partial.replace(output)
        print(
            f"Receipt year {receipt_year} complete: {pages_read} page(s), "
            f"{rows_written}/{total_records} row(s). Saved: {output}",
            flush=True,
        )
        return 0

    print(
        f"Receipt year {receipt_year} partial: {pages_read} page(s), "
        f"{rows_written}/{total_records} row(s). Partial file: {partial}",
        flush=True,
    )
    return 3


def main() -> int:
    args = parse_args()
    try:
        api_key = read_api_key()
        existing = [
            args.output_dir / f"api_receipt_{year}.jsonl"
            for year in args.receipt_years
            if (args.output_dir / f"api_receipt_{year}.jsonl").exists()
        ]
        if existing:
            paths = ", ".join(str(path) for path in existing)
            raise FileExistsError(f"Output already exists; refusing to overwrite: {paths}")

        exit_code = 0
        for receipt_year in args.receipt_years:
            print(f"Starting receipt year {receipt_year}.", flush=True)
            year_exit_code = collect_year(
                api_key,
                receipt_year,
                args.page_size,
                args.max_pages,
                args.output_dir,
            )
            exit_code = max(exit_code, year_exit_code)
        return exit_code
    except (FileExistsError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
