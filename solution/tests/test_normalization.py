import unittest

from solution.normalization import normalize_amount, normalize_date, normalize_phone


class NormalizationTests(unittest.TestCase):
    def test_mobile_prefixes_and_punctuation(self):
        for raw in ("9876543210", "+91 98765 43210", "09876543210", "91-98765-43210",
                    "(+91) 9876543210", "+919876543210", "९८७६५४३२१०"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw), "+919876543210")
        for raw in ("", "N/A", "123456789", "5876543210", "+1 415 555 0134",
                    "91987654321", "+91 09876543210", "022-2345-6789"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_phone(raw))

    def test_dates_are_day_first_and_validate_the_calendar(self):
        valid = {
            "2026-02-01": "2026-02-01", "01/02/2026": "2026-02-01",
            "01-02-2026": "2026-02-01", "01-fEb-2026": "2026-02-01",
            "29/02/2024": "2024-02-29",
        }
        for raw, expected in valid.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_date(raw), expected)
        for raw in ("31/02/2026", "29/02/2026", "2026-13-01", "13/01-2026",
                    "2026/02/01", "1/2/2026", "01-Foo-2026", "soon"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_date(raw))

    def test_amounts_use_decimal_rounding_and_validate_grouping(self):
        valid = {
            "₹1,234.565": "1234.57", "rs. 9,999.995": "10000.00", "RS500": "500.00",
            "inR 12,34,567.00": "1234567.00", ".005": "0.01", "0": "0.00",
            "999999999999999999999999999999.999": "1000000000000000000000000000000.00",
        }
        for raw, expected in valid.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_amount(raw), expected)
        for raw in ("-0.001", "NaN", "Infinity", "1e3", "1,2,3", "1,23", "2.1,2",
                    "Rs.", "10 000", "USD 20", "twelve thousand"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_amount(raw))
