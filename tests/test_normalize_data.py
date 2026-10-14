import csv
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts.normalize_data import (
    API_FIELDS,
    OUTPUT_FIELDS,
    normalize,
    normalize_date,
    normalize_numeric,
    normalize_row,
)


class ValueNormalizationTests(unittest.TestCase):
    def test_normalizes_contract_dates_to_yyyymmdd(self) -> None:
        self.assertEqual(normalize_date("2023. 1. 2."), "20230102")
        self.assertEqual(normalize_date("20230102"), "20230102")
        self.assertEqual(normalize_date(""), "")

    def test_rejects_invalid_contract_dates(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid contract date"):
            normalize_date("2023-02-30")

    def test_normalizes_numeric_values_without_float_rounding(self) -> None:
        self.assertEqual(normalize_numeric("1.0"), "1")
        self.assertEqual(normalize_numeric("57.550"), "57.55")
        self.assertEqual(normalize_numeric("1,800"), "1800")
        self.assertEqual(normalize_numeric(""), "")

    def test_preserves_zero_padded_lot_numbers(self) -> None:
        source = {field: "" for field in API_FIELDS}
        source.update({"MNO": "0102", "SNO": "0024"})

        result = normalize_row(source, "api.jsonl", 1, is_api=True)

        self.assertEqual(result["MNO"], "0102")
        self.assertEqual(result["SNO"], "0024")

    def test_keeps_receipt_and_contract_dates_distinct(self) -> None:
        source = {field: "" for field in API_FIELDS}
        source.update({"RCPT_YR": "2026", "CTRT_DAY": "20251231"})

        result = normalize_row(source, "api.jsonl", 4, is_api=True)

        self.assertEqual(result["RCPT_YR"], "2026")
        self.assertEqual(result["CTRT_DAY"], "20251231")


class NormalizeFileTests(unittest.TestCase):
    def test_normalizes_csv_and_jsonl_to_same_schema_without_deduping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = root / "raw"
            raw.mkdir()
            csv_path = raw / "source.csv"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=[
                        "접수년도",
                        "계약일",
                        "전월세구분",
                        "보증금(만원)",
                        "임대료(만원)",
                        "자치구코드",
                        "자치구명",
                        "법정동코드",
                        "법정동명",
                        "지번구분코드",
                        "지번구분",
                        "본번",
                        "부번",
                        "층",
                        "임대면적",
                        "건물명",
                        "건축년도",
                        "건물용도",
                        "계약기간",
                        "신규계약구분",
                        "갱신청구권사용",
                        "종전보증금",
                        "종전임대료",
                    ],
                )
                writer.writeheader()
                row = {
                    "접수년도": "2025",
                    "계약일": "2025. 1. 2.",
                    "전월세구분": "전세",
                    "보증금(만원)": "1,800",
                    "임대료(만원)": "0",
                    "자치구코드": "11620",
                    "자치구명": "관악구",
                    "법정동코드": "10200",
                    "법정동명": "신림동",
                    "지번구분코드": "1",
                    "지번구분": "대지",
                    "본번": "0102",
                    "부번": "0024",
                    "층": "1.0",
                    "임대면적": "57.550",
                    "건물명": "테스트",
                    "건축년도": "2002",
                    "건물용도": "연립다세대",
                    "계약기간": "25.01~27.01",
                    "신규계약구분": "신규",
                    "갱신청구권사용": "",
                    "종전보증금": "",
                    "종전임대료": "",
                }
                writer.writerow(row)
                writer.writerow(row)

            api_path = raw / "api.jsonl"
            api_row = {
                "RCPT_YR": "2025",
                "CTRT_DAY": "20250102",
                "RENT_SE": "전세",
                "GRFE": "1800",
                "RTFE": "0",
                "CGG_CD": "11620",
                "CGG_NM": "관악구",
                "STDG_CD": "10200",
                "STDG_NM": "신림동",
                "LOTNO_SE": "1",
                "LOTNO_SE_NM": "대지",
                "MNO": "0102",
                "SNO": "0024",
                "FLR": 1.0,
                "RENT_AREA": 57.55,
                "BLDG_NM": "테스트",
                "ARCH_YR": 2002,
                "BLDG_USG": "연립다세대",
                "CTRT_PRD": "25.01~27.01",
                "NEW_UPDT_YN": "신규",
                "CTRT_UPDT_USE_YN": "",
                "BFR_GRFE": "",
                "BFR_RTFE": "",
            }
            api_path.write_text(json.dumps(api_row) + "\n", encoding="utf-8")

            output = root / "processed" / "standardized.csv.gz"
            self.assertEqual(normalize(raw, output), 0)

            with gzip.open(output, "rt", encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)

        self.assertEqual(reader.fieldnames, list(OUTPUT_FIELDS))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["CTRT_DAY"], "20250102")
        self.assertEqual(rows[0]["GRFE"], "1800")
        self.assertEqual(rows[0]["MNO"], "0102")
        self.assertEqual(rows[0]["SOURCE_FILE"], "api.jsonl")
        self.assertEqual(rows[1]["SOURCE_FILE"], "source.csv")
        self.assertEqual(rows[2]["SOURCE_ROW"], "3")


if __name__ == "__main__":
    unittest.main()
