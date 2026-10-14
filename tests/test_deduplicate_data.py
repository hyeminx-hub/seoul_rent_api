import csv
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts.deduplicate_data import deduplicate
from scripts.normalize_data import OUTPUT_FIELDS


def make_row(**overrides: str) -> dict[str, str]:
    row = {field: "" for field in OUTPUT_FIELDS}
    row.update(
        {
            "RCPT_YR": "2024",
            "CTRT_DAY": "20240102",
            "RENT_SE": "전세",
            "GRFE": "1800",
            "RTFE": "0",
            "SOURCE_FILE": "api.jsonl",
            "SOURCE_ROW": "1",
        }
    )
    row.update(overrides)
    return row


def write_standardized(path: Path, rows: list[dict[str, str]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


class DeduplicateTests(unittest.TestCase):
    def test_removes_only_redundant_cross_source_copies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "standardized.csv.gz"
            output_path = root / "deduplicated.csv.gz"
            removed_path = root / "removed.csv.gz"
            audit_path = root / "audit.csv.gz"
            rows = [
                make_row(SOURCE_FILE="api.jsonl", SOURCE_ROW="1"),
                make_row(SOURCE_FILE="api.jsonl", SOURCE_ROW="2"),
                make_row(
                    RCPT_YR="2024",
                    SOURCE_FILE="annual.csv",
                    SOURCE_ROW="2",
                ),
                make_row(
                    CTRT_DAY="20240103",
                    SOURCE_FILE="annual.csv",
                    SOURCE_ROW="3",
                ),
                make_row(
                    CTRT_DAY="20240103",
                    SOURCE_FILE="annual.csv",
                    SOURCE_ROW="4",
                ),
                make_row(
                    CTRT_DAY="20240104",
                    SOURCE_FILE="annual.csv",
                    SOURCE_ROW="5",
                ),
                make_row(
                    RCPT_YR="2025",
                    SOURCE_FILE="annual.csv",
                    SOURCE_ROW="6",
                ),
            ]
            write_standardized(input_path, rows)

            summary = deduplicate(input_path, output_path, removed_path, audit_path)

            self.assertEqual(
                summary,
                {
                    "input_rows": 7,
                    "output_rows": 6,
                    "overlap_groups": 1,
                    "removed_rows": 1,
                    "intra_source_repeat_groups_preserved": 2,
                    "intra_source_repeat_rows_preserved": 2,
                },
            )
            kept = read_csv(output_path)
            removed = read_csv(removed_path)
            audit = read_csv(audit_path)
            self.assertEqual(len(kept), 6)
            self.assertEqual(len(removed), 1)
            self.assertEqual(removed[0]["SOURCE_FILE"], "annual.csv")
            self.assertEqual(removed[0]["SOURCE_ROW"], "2")
            self.assertEqual(len([row for row in kept if row["CTRT_DAY"] == "20240103"]), 2)
            self.assertEqual(
                len([row for row in kept if row["RCPT_YR"] == "2025"]), 1
            )
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["SELECTED_SOURCE"], "api.jsonl")
            self.assertEqual(json.loads(audit[0]["RECEIPT_YEARS"]), ["2024"])
            self.assertEqual(json.loads(audit[0]["SOURCE_COUNTS"]), {
                "annual.csv": 1,
                "api.jsonl": 2,
            })

    def test_refuses_to_overwrite_any_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "standardized.csv.gz"
            output_path = root / "deduplicated.csv.gz"
            removed_path = root / "removed.csv.gz"
            audit_path = root / "audit.csv.gz"
            write_standardized(
                input_path,
                [make_row(SOURCE_FILE="api.jsonl")],
            )
            output_path.write_text("existing", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                deduplicate(input_path, output_path, removed_path, audit_path)


if __name__ == "__main__":
    unittest.main()
