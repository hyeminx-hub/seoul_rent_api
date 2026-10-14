import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.fetch_api import collect_year, fetch_page, parse_page


class ParsePageTests(unittest.TestCase):
    def test_reads_total_and_rows(self) -> None:
        payload = {
            "tbLnOpendataRentV": {
                "list_total_count": 1,
                "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
                "row": [{"RCPT_YR": "2025", "CTRT_DAY": "20250315"}],
            }
        }

        total, rows = parse_page(payload)

        self.assertEqual(total, 1)
        self.assertEqual(rows, [{"RCPT_YR": "2025", "CTRT_DAY": "20250315"}])

    def test_rejects_service_api_error(self) -> None:
        payload = {
            "tbLnOpendataRentV": {
                "RESULT": {"CODE": "ERROR-310", "MESSAGE": "Invalid service"},
                "list_total_count": 0,
                "row": [],
            }
        }

        with self.assertRaisesRegex(ValueError, "ERROR-310"):
            parse_page(payload)

    def test_treats_no_data_response_as_empty_result(self) -> None:
        payload = {
            "RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}
        }

        self.assertEqual(parse_page(payload), (0, []))


class CollectionTests(unittest.TestCase):
    def test_collects_all_pages_for_receipt_year_without_contract_year_filter(self) -> None:
        pages = [
            {
                "tbLnOpendataRentV": {
                    "list_total_count": 3,
                    "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
                    "row": [
                        {"RCPT_YR": "2025", "CTRT_DAY": "20241231"},
                        {"RCPT_YR": "2025", "CTRT_DAY": "20250101"},
                    ],
                }
            },
            {
                "tbLnOpendataRentV": {
                    "list_total_count": 3,
                    "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
                    "row": [{"RCPT_YR": "2025", "CTRT_DAY": "20260101"}],
                }
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with patch("scripts.fetch_api.fetch_page", side_effect=pages) as fetch:
                with redirect_stdout(io.StringIO()):
                    status = collect_year(
                        "test-key",
                        receipt_year=2025,
                        page_size=2,
                        max_pages=None,
                        output_dir=output_dir,
                    )

            output = output_dir / "api_receipt_2025.jsonl"
            saved_rows = [json.loads(line) for line in output.read_text().splitlines()]

        self.assertEqual(status, 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(
            saved_rows,
            [
                {"RCPT_YR": "2025", "CTRT_DAY": "20241231"},
                {"RCPT_YR": "2025", "CTRT_DAY": "20250101"},
                {"RCPT_YR": "2025", "CTRT_DAY": "20260101"},
            ],
        )

    def test_page_limit_leaves_partial_file_and_returns_partial_status(self) -> None:
        page = {
            "tbLnOpendataRentV": {
                "list_total_count": 3,
                "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
                "row": [
                    {"RCPT_YR": "2024", "CTRT_DAY": "20240101"},
                    {"RCPT_YR": "2024", "CTRT_DAY": "20240102"},
                ],
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with patch("scripts.fetch_api.fetch_page", return_value=page):
                with redirect_stdout(io.StringIO()):
                    status = collect_year(
                        "test-key",
                        receipt_year=2024,
                        page_size=2,
                        max_pages=1,
                        output_dir=output_dir,
                    )

            self.assertFalse((output_dir / "api_receipt_2024.jsonl").exists())
            self.assertTrue(
                (output_dir / "api_receipt_2024.jsonl.part").exists()
            )

        self.assertEqual(status, 3)

    def test_rejects_rows_outside_requested_receipt_year(self) -> None:
        page = {
            "tbLnOpendataRentV": {
                "list_total_count": 1,
                "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
                "row": [{"RCPT_YR": "2024", "CTRT_DAY": "20240101"}],
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("scripts.fetch_api.fetch_page", return_value=page):
                with self.assertRaisesRegex(RuntimeError, "outside receipt year"):
                    collect_year(
                        "test-key",
                        receipt_year=2025,
                        page_size=1000,
                        max_pages=None,
                        output_dir=Path(temp_dir),
                    )

    def test_page_request_uses_positional_receipt_year(self) -> None:
        with patch("scripts.fetch_api.urlopen") as open_url:
            with patch(
                "scripts.fetch_api.json.load",
                return_value={"RESULT": {"CODE": "INFO-200"}},
            ):
                fetch_page("test-key", 1, 1000, 2025)

        request = open_url.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://openapi.seoul.go.kr:8088/test-key/json/"
            "tbLnOpendataRentV/1/1000/2025/",
        )


if __name__ == "__main__":
    unittest.main()
