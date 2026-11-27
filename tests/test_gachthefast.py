import unittest

from gachthefast_logic import calculate_credit, claim_pending_topup


class FakeUpdateResult:
    def __init__(self, matched_count):
        self.matched_count = matched_count


class FakeTopupCollection:
    """Small fake proving the claim helper uses a conditional atomic update."""
    def __init__(self):
        self.status = "pending"

    def update_one(self, query, update):
        if query.get("status") != self.status:
            return FakeUpdateResult(0)
        self.status = update["$set"]["status"]
        return FakeUpdateResult(1)


class GachTheFastCreditTests(unittest.TestCase):
    def test_viettel_examples_at_23_percent_target(self):
        cases = [
            (10_000, 8_050, 7_700, 350),
            (20_000, 16_000, 15_400, 600),
            (30_000, 24_000, 23_100, 900),
            (50_000, 43_000, 38_500, 4_500),
            (100_000, 80_500, 77_000, 3_500),
        ]
        for face_value, gtf_amount, expected_credit, expected_margin in cases:
            with self.subTest(face_value=face_value):
                credit, margin = calculate_credit(
                    face_value, 23, gtf_amount
                )
                self.assertEqual(credit, expected_credit)
                self.assertEqual(margin, expected_margin)

    def test_negative_margin_is_reported_not_hidden(self):
        credit, margin = _calculate_gachthefast_credit(100_000, 23, 70_000)
        self.assertEqual(credit, 77_000)
        self.assertEqual(margin, -7_000)

    def test_zero_or_invalid_face_value_rejected(self):
        for face_value in (0, -1, None, "invalid"):
            with self.subTest(face_value=face_value):
                with self.assertRaises(ValueError):
                    _calculate_gachthefast_credit(face_value, 23, 8_050)

    def test_invalid_target_discount_rejected(self):
        for discount in (-0.1, 100, 120):
            with self.subTest(discount=discount):
                with self.assertRaises(ValueError):
                    _calculate_gachthefast_credit(10_000, discount, 8_050)

    def test_only_first_callback_can_claim_pending_topup(self):
        collection = FakeTopupCollection()
        first = claim_pending_topup(collection, "topup-1", "now")
        second = _claim_gachthefast_topup("topup-1", collection)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(collection.status, "processing")



if __name__ == "__main__":
    unittest.main()
