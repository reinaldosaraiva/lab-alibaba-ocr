"""Acceptance checks for the MR exercise; keep these unchanged in the branch."""

import unittest

from price import discounted_price


class DiscountedPriceTests(unittest.TestCase):
    def test_applies_discount(self) -> None:
        self.assertEqual(discounted_price(12_000, 33), 8_040)

    def test_boundaries(self) -> None:
        self.assertEqual(discounted_price(12_000, 0), 12_000)
        self.assertEqual(discounted_price(12_000, 100), 0)

    def test_rejects_invalid_input(self) -> None:
        for price, discount in [(-1, 10), (100, -1), (100, 101)]:
            with (
                self.subTest(price=price, discount=discount),
                self.assertRaises(ValueError),
            ):
                discounted_price(price, discount)


if __name__ == "__main__":
    unittest.main()
