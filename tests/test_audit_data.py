import unittest

from scripts.audit_data import contract_year, is_valid_amount


class ContractYearTests(unittest.TestCase):
    def test_extracts_year_from_supported_date_formats(self) -> None:
        self.assertEqual(contract_year("2021. 1. 2."), 2021)
        self.assertEqual(contract_year("2021-01-02"), 2021)
        self.assertEqual(contract_year("20210102"), 2021)

    def test_rejects_invalid_dates(self) -> None:
        self.assertIsNone(contract_year("2021-02-30"))
        self.assertIsNone(contract_year("not a date"))


class AmountValidationTests(unittest.TestCase):
    def test_accepts_blank_and_numeric_values(self) -> None:
        self.assertTrue(is_valid_amount(""))
        self.assertTrue(is_valid_amount("12,345"))
        self.assertTrue(is_valid_amount("0"))

    def test_rejects_non_numeric_values(self) -> None:
        self.assertFalse(is_valid_amount("not available"))


if __name__ == "__main__":
    unittest.main()
