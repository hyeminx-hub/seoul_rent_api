import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SERVICE = "tbLnOpendataRentV"
API_BASE_URL = "http://openapi.seoul.go.kr:8088"


def main() -> int:
    api_key = os.environ.get("SEOUL_OPEN_DATA_API_KEY", "").strip()
    if not api_key:
        print(
            "SEOUL_OPEN_DATA_API_KEY is not set. Load it from your local .env file.",
            file=sys.stderr,
        )
        return 2

    url = f"{API_BASE_URL}/{api_key}/json/{SERVICE}/1/5/"
    request = Request(url, headers={"User-Agent": "seoul-rent-analysis/0.1"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        print(f"Seoul Open Data API returned HTTP {exc.code}.", file=sys.stderr)
        return 1
    except URLError as exc:
        print(
            f"Could not reach Seoul Open Data API ({type(exc.reason).__name__}).",
            file=sys.stderr,
        )
        return 1
    except TimeoutError:
        print("Seoul Open Data API request timed out.", file=sys.stderr)
        return 1
    except (UnicodeDecodeError, json.JSONDecodeError):
        print("Seoul Open Data API returned invalid JSON.", file=sys.stderr)
        return 1

    if not isinstance(payload, dict):
        print("API response has an unexpected top-level structure.", file=sys.stderr)
        return 1

    result_set = payload.get(SERVICE)
    if not isinstance(result_set, dict):
        print("API response does not contain the expected service object.", file=sys.stderr)
        return 1

    result = result_set.get("RESULT")
    if not isinstance(result, dict):
        print("API response does not contain a valid result status.", file=sys.stderr)
        return 1

    if result.get("CODE") != "INFO-000":
        print(
            f"Seoul Open Data API error {result.get('CODE', 'unknown')}: "
            f"{result.get('MESSAGE', 'No message returned')}",
            file=sys.stderr,
        )
        return 1

    rows = result_set.get("row")
    if not isinstance(rows, list):
        print("API response does not contain a row list.", file=sys.stderr)
        return 1
    if any(not isinstance(row, dict) for row in rows):
        print("API response contains an invalid row.", file=sys.stderr)
        return 1
    if not rows:
        print("API response contains no sample rows.", file=sys.stderr)
        return 1

    required_fields = {"CTRT_DAY", "RENT_SE", "GRFE", "RTFE", "CGG_NM"}
    missing_fields = required_fields.difference(rows[0])
    if missing_fields:
        print(
            "API response is missing required fields: "
            f"{', '.join(sorted(missing_fields))}",
            file=sys.stderr,
        )
        return 1

    contract_dates = sorted(
        row["CTRT_DAY"]
        for row in rows
        if isinstance(row.get("CTRT_DAY"), str)
        and len(row["CTRT_DAY"]) == 8
        and row["CTRT_DAY"].isdigit()
    )
    rent_types = sorted(
        {row["RENT_SE"] for row in rows if isinstance(row.get("RENT_SE"), str)}
    )

    print(f"API check succeeded; inspected {len(rows)} row(s).")
    print(f"Available records reported by API: {result_set.get('list_total_count', 'unknown')}")
    print(f"Response fields: {', '.join(sorted(rows[0]))}")
    print(
        "Sample contract-date range: "
        f"{contract_dates[0] if contract_dates else 'unknown'} - "
        f"{contract_dates[-1] if contract_dates else 'unknown'}"
    )
    print(f"Sample rent types: {', '.join(rent_types) if rent_types else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
