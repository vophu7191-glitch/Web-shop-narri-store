import copy
import hashlib
import pathlib
import threading
import unittest

from gachthefast_logic import (
    calculate_credit,
    claim_pending_topup,
    credit_user_once,
    handle_callback,
    parse_callback_payload,
    parse_strict_int,
    record_processing_error,
    verify_callback_sign,
)

PARTNER_KEY = "partner-secret"
CODE = "123456789012"
SERIAL = "SER0001"


def sign(code=CODE, serial=SERIAL, key=PARTNER_KEY):
    return hashlib.md5((key + code + serial).encode("utf-8")).hexdigest()


class FakeUpdateResult:
    def __init__(self, matched_count, modified_count=None, upserted_id=None):
        self.matched_count = matched_count
        self.modified_count = matched_count if modified_count is None else modified_count
        self.upserted_id = upserted_id


class FakeCollection:
    """Tiny in-memory subset of PyMongo semantics used by the callback code.

    Supports equality filters (None also matches a missing field), {"$ne": x} on
    scalars or arrays, and $set / $inc / $addToSet / $setOnInsert with upsert.
    A lock makes each update_one atomic, like a single-document MongoDB update.
    """

    def __init__(self, docs=None):
        self.docs = [copy.deepcopy(d) for d in (docs or [])]
        self.lock = threading.Lock()

    @staticmethod
    def _matches(doc, query):
        for key, cond in query.items():
            actual = doc.get(key)
            if isinstance(cond, dict) and "$ne" in cond:
                if isinstance(actual, list):
                    if cond["$ne"] in actual:
                        return False
                elif actual == cond["$ne"]:
                    return False
            elif cond is None:
                if actual is not None:
                    return False
            elif actual != cond:
                return False
        return True

    def find_one(self, query, projection=None):
        with self.lock:
            for doc in self.docs:
                if self._matches(doc, query):
                    return copy.deepcopy(doc)
        return None

    def update_one(self, query, update, upsert=False):
        with self.lock:
            for doc in self.docs:
                if self._matches(doc, query):
                    before = copy.deepcopy(doc)
                    for key, value in update.get("$set", {}).items():
                        doc[key] = copy.deepcopy(value)
                    for key, value in update.get("$inc", {}).items():
                        doc[key] = doc.get(key, 0) + value
                    for key, value in update.get("$addToSet", {}).items():
                        doc.setdefault(key, [])
                        if value not in doc[key]:
                            doc[key].append(value)
                    return FakeUpdateResult(1, int(before != doc))
            if upsert:
                doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
                doc.update(copy.deepcopy(update.get("$setOnInsert", {})))
                doc.update(copy.deepcopy(update.get("$set", {})))
                self.docs.append(doc)
                return FakeUpdateResult(0, 0, doc.get("_id"))
            return FakeUpdateResult(0, 0)


class CallbackHarness:
    def __init__(self, discount=23, declared_value=10_000):
        self.topups = FakeCollection([{
            "_id": "topup-1", "user_id": "user-1", "username": "alice",
            "telco": "VIETTEL", "code": CODE, "serial": SERIAL,
            "declared_value": declared_value, "value": None, "credited_amount": None,
            "request_id": "req-1", "status": "pending",
        }])
        self.users = FakeCollection([
            {"_id": "user-1", "balance": 0, "processed_card_topup_ids": []}
        ])
        self.recharges = FakeCollection()
        self.discounts = {"viettel": discount}
        self.relayed = []
        self.successes = []
        self.failures = []
        self._ids = 0

    def new_id(self):
        self._ids += 1
        return f"rc-{self._ids}"

    def payload(self, **overrides):
        data = {
            "request_id": "req-1", "status": 1, "value": 10_000, "amount": 8_050,
            "declared_value": 10_000, "code": CODE, "serial": SERIAL,
            "trans_id": "gtf-1", "callback_sign": sign(), "message": "OK",
        }
        data.update(overrides)
        return data

    def call(self, data):
        return handle_callback(
            data, topups=self.topups, users=self.users, recharges=self.recharges,
            partner_key=PARTNER_KEY, discounts=self.discounts,
            now=lambda: "now", new_id=self.new_id,
            relay=self.relayed.append,
            on_success=lambda *args: self.successes.append(args),
            on_failed=lambda *args: self.failures.append(args),
        )

    @property
    def topup(self):
        return self.topups.find_one({"_id": "topup-1"})

    @property
    def balance(self):
        return self.users.find_one({"_id": "user-1"})["balance"]


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
                credit, margin = calculate_credit(face_value, 23, gtf_amount)
                self.assertEqual(credit, expected_credit)
                self.assertEqual(margin, expected_margin)

    def test_negative_margin_is_reported_not_hidden(self):
        credit, margin = calculate_credit(100_000, 23, 70_000)
        self.assertEqual(credit, 77_000)
        self.assertEqual(margin, -7_000)

    def test_zero_or_invalid_face_value_rejected(self):
        for face_value in (0, -1, None, "invalid", 50_000.5, "50000.5", True):
            with self.subTest(face_value=face_value):
                with self.assertRaises(ValueError):
                    calculate_credit(face_value, 23, 8_050)

    def test_invalid_target_discount_rejected(self):
        for discount in (-0.1, 100, 120, float("nan")):
            with self.subTest(discount=discount):
                with self.assertRaises(ValueError):
                    calculate_credit(10_000, discount, 8_050)

    def test_strict_int_parsing(self):
        accepted = {10_000: 10_000, "10000": 10_000, " 8050 ": 8_050, "8100.0": 8_100, 8_100.0: 8_100}
        for raw, expected in accepted.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_strict_int(raw), expected)
        for raw in (None, "", "abc", 50_000.5, "50000.5", "NaN", "inf", float("inf"), True, [1]):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_strict_int(raw))

    def test_wallet_credit_is_idempotent_when_callback_retries(self):
        users = FakeCollection([{"_id": "user-1", "balance": 0, "processed_card_topup_ids": []}])
        first = credit_user_once(users, "user-1", "topup-1", 7_700)
        second = credit_user_once(users, "user-1", "topup-1", 7_700)
        self.assertEqual(first, (True, True))
        self.assertEqual(second, (False, True))
        self.assertEqual(users.find_one({"_id": "user-1"})["balance"], 7_700)

    def test_only_first_callback_can_claim_pending_topup(self):
        collection = FakeCollection([{"_id": "topup-1", "status": "pending"}])
        self.assertTrue(claim_pending_topup(collection, "topup-1", "now"))
        self.assertFalse(claim_pending_topup(collection, "topup-1", "now"))
        self.assertEqual(collection.find_one({"_id": "topup-1"})["status"], "processing")


class CallbackSignatureTests(unittest.TestCase):
    def test_valid_signature_accepted_case_insensitively(self):
        data = {"code": CODE, "serial": SERIAL, "callback_sign": sign().upper()}
        self.assertTrue(verify_callback_sign(PARTNER_KEY, data))

    def test_missing_or_invalid_signature_rejected(self):
        cases = [
            {"code": CODE, "serial": SERIAL},
            {"code": CODE, "serial": SERIAL, "callback_sign": "deadbeef"},
            {"code": CODE, "serial": SERIAL, "callback_sign": sign(key="other")},
            {"serial": SERIAL, "callback_sign": sign()},
            {"code": CODE, "callback_sign": sign()},
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assertFalse(verify_callback_sign(PARTNER_KEY, data))
        self.assertFalse(verify_callback_sign("", {"code": CODE, "serial": SERIAL,
                                                    "callback_sign": sign(key="")}))

    def test_invalid_signature_never_credits_or_relays(self):
        for bad in ({"callback_sign": "bad"}, {"callback_sign": None}):
            for request_id in ("req-1", "unknown"):
                with self.subTest(bad=bad, request_id=request_id):
                    h = CallbackHarness()
                    body, code = h.call(h.payload(request_id=request_id, **bad))
                    self.assertEqual((body, code), ({"status": "invalid_signature"}, 403))
                    self.assertEqual(h.balance, 0)
                    self.assertEqual(h.topup["status"], "pending")
                    self.assertEqual(h.relayed, [])

    def test_signature_for_another_card_cannot_settle_this_topup(self):
        h = CallbackHarness()
        body, code = h.call(h.payload(code="999", serial="888", callback_sign=sign("999", "888")))
        self.assertEqual(code, 403)
        self.assertEqual(h.balance, 0)
        self.assertEqual(h.topup["status"], "pending")


class CallbackFlowTests(unittest.TestCase):
    def test_status_1_credits_from_value_and_records_amount(self):
        h = CallbackHarness()
        self.assertEqual(h.call(h.payload()), ({"status": "ok"}, 200))
        topup = h.topup
        self.assertEqual(h.balance, 7_700)
        self.assertEqual(topup["status"], "success")
        self.assertEqual(topup["credited_amount"], 7_700)
        self.assertEqual(topup["gachthefast_amount"], 8_050)
        self.assertEqual(topup["shop_margin"], 350)
        self.assertEqual(len(h.recharges.docs), 1)
        self.assertEqual(h.recharges.docs[0]["amount"], 7_700)
        self.assertEqual(len(h.successes), 1)

    def test_status_2_uses_callback_value_not_declared_value(self):
        h = CallbackHarness(declared_value=50_000)
        body, _ = h.call(h.payload(status=2, value=10_000, amount=8_050))
        self.assertEqual(body, {"status": "ok"})
        self.assertEqual(h.balance, 7_700)
        self.assertEqual(h.topup["face_value"], 10_000)

    def test_success_without_valid_value_goes_to_review_without_fallback(self):
        for value in (None, "", 0, -10_000, "abc", 10_000.5, "10000.5"):
            with self.subTest(value=value):
                h = CallbackHarness()
                body, _ = h.call(h.payload(value=value))
                self.assertEqual(body, {"status": "review"})
                self.assertEqual(h.topup["review_reason"], "successful_callback_missing_or_invalid_value")
                self.assertEqual(h.balance, 0)
                self.assertEqual(h.recharges.docs, [])

    def test_success_without_valid_amount_goes_to_review(self):
        for amount in (None, 0, "8050.5", "x"):
            with self.subTest(amount=amount):
                h = CallbackHarness()
                body, _ = h.call(h.payload(amount=amount))
                self.assertEqual(body, {"status": "review"})
                self.assertEqual(h.topup["review_reason"], "successful_callback_missing_positive_payout")
                self.assertEqual(h.balance, 0)

    def test_negative_margin_goes_to_review(self):
        h = CallbackHarness()
        body, _ = h.call(h.payload(amount=7_000))
        self.assertEqual(body, {"status": "review"})
        self.assertEqual(h.topup["review_reason"], "gachthefast_payout_below_customer_credit")
        self.assertEqual(h.balance, 0)

    def test_status_99_never_claims_or_credits(self):
        h = CallbackHarness()
        body, _ = h.call(h.payload(status=99, value=None, amount=None))
        self.assertEqual(body, {"status": "pending"})
        self.assertEqual(h.topup["status"], "pending")
        self.assertEqual(h.balance, 0)
        # A later final callback still works.
        self.assertEqual(h.call(h.payload())[0], {"status": "ok"})
        self.assertEqual(h.balance, 7_700)

    def test_failed_statuses_never_credit(self):
        for status in (3, 4, 100, "3", "100"):
            with self.subTest(status=status):
                h = CallbackHarness()
                body, _ = h.call(h.payload(status=status))
                self.assertEqual(body, {"status": "ok"})
                self.assertEqual(h.topup["status"], "failed")
                self.assertEqual(h.balance, 0)
                self.assertEqual(h.recharges.docs, [])
                self.assertEqual(len(h.failures), 1)

    def test_unknown_status_goes_to_review(self):
        h = CallbackHarness()
        body, _ = h.call(h.payload(status=7))
        self.assertEqual(body, {"status": "review"})
        self.assertEqual(h.topup["review_reason"], "unknown_callback_status")
        self.assertEqual(h.balance, 0)

    def test_unknown_request_is_relayed_after_signature_check(self):
        h = CallbackHarness()
        data = h.payload(request_id="bot-req")
        self.assertEqual(h.call(data), ({"status": "ignored"}, 200))
        self.assertEqual(h.relayed, [data])
        self.assertEqual(h.topup["status"], "pending")


class DuplicateCallbackTests(unittest.TestCase):
    def test_repeated_identical_callback_credits_once(self):
        h = CallbackHarness()
        self.assertEqual(h.call(h.payload())[0], {"status": "ok"})
        self.assertEqual(h.call(h.payload())[0], {"status": "already_processed"})
        self.assertEqual(h.balance, 7_700)
        self.assertEqual(len(h.recharges.docs), 1)
        self.assertEqual(len(h.successes), 1)

    def test_concurrent_identical_callbacks_credit_once(self):
        h = CallbackHarness()
        results = []
        threads = [threading.Thread(target=lambda: results.append(h.call(h.payload())))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(h.balance, 7_700)
        self.assertEqual(len(h.recharges.docs), 1)
        self.assertEqual(len(h.successes), 1)
        self.assertEqual(h.topup["status"], "success")
        self.assertEqual(len(results), 8)

    def test_conflicting_callback_after_terminal_does_not_overwrite(self):
        h = CallbackHarness()
        h.call(h.payload())
        before = h.topup
        for conflicting in (h.payload(status=3), h.payload(value=100_000, amount=80_500),
                            h.payload(status=2, amount=9_000)):
            with self.subTest(conflicting=conflicting):
                self.assertEqual(h.call(conflicting)[0], {"status": "already_processed"})
                self.assertEqual(h.topup, before)
        self.assertEqual(h.balance, 7_700)
        self.assertEqual(len(h.recharges.docs), 1)

    def test_conflict_wins_before_settlement_fence_no_wallet_side_effects(self):
        h = CallbackHarness()
        first = h.payload()
        entered_fence = threading.Event()
        release_fence = threading.Event()
        original_update = h.topups.update_one

        def pause_first_settlement(query, update, upsert=False):
            if query.get("status") == "processing" and "callback_result" in query:
                entered_fence.set()
                self.assertTrue(release_fence.wait(timeout=5))
            return original_update(query, update, upsert=upsert)

        h.topups.update_one = pause_first_settlement
        results = []
        worker = threading.Thread(target=lambda: results.append(h.call(first)))
        worker.start()
        self.assertTrue(entered_fence.wait(timeout=5))

        # B changes processing -> review while A is paused before its atomic fence.
        conflict = h.payload(value=100_000, amount=80_500)
        self.assertEqual(h.call(conflict)[0], {"status": "review"})
        self.assertEqual(h.topup["status"], "review")

        release_fence.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(h.balance, 0)
        self.assertEqual(h.recharges.docs, [])
        self.assertEqual(h.topup["status"], "review")
        self.assertEqual(results, [({"status": "already_processed"}, 200)])

    def test_conflict_cannot_revoke_callback_after_settlement_fence(self):
        h = CallbackHarness()
        entered_credit = threading.Event()
        release_credit = threading.Event()
        original_credit = __import__("gachthefast_logic").credit_user_once

        def pause_credit(*args, **kwargs):
            entered_credit.set()
            self.assertTrue(release_credit.wait(timeout=5))
            return original_credit(*args, **kwargs)

        import gachthefast_logic
        gachthefast_logic.credit_user_once = pause_credit
        results = []
        worker = threading.Thread(target=lambda: results.append(h.call(h.payload())))
        try:
            worker.start()
            self.assertTrue(entered_credit.wait(timeout=5))
            self.assertEqual(h.topup["status"], "settling")
            # Conflicting B cannot move settling to review or interfere with A.
            self.assertEqual(h.call(h.payload(value=100_000, amount=80_500))[0],
                             {"status": "already_processed"})
            self.assertEqual(h.topup["status"], "settling")
            release_credit.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        finally:
            release_credit.set()
            gachthefast_logic.credit_user_once = original_credit
        self.assertEqual(h.balance, 7_700)
        self.assertEqual(len(h.recharges.docs), 1)
        self.assertEqual(h.topup["status"], "success")


class RecoveryPolicyTests(unittest.TestCase):
    def test_processing_error_keeps_topup_recoverable(self):
        h = CallbackHarness()
        from gachthefast_logic import normalize_callback_result
        first = h.payload()
        claim_pending_topup(h.topups, "topup-1", "now", normalize_callback_result(first))
        record_processing_error(h.topups, "req-1", RuntimeError("boom"), "now")
        self.assertEqual(h.topup["status"], "processing")
        self.assertEqual(h.topup["last_error"], "boom")
        # The provider's retry of the same signed callback completes the top-up.
        self.assertEqual(h.call(first)[0], {"status": "ok"})
        self.assertEqual(h.topup["status"], "success")
        self.assertEqual(h.balance, 7_700)

    def test_crash_during_settlement_is_fenced_for_manual_reconciliation(self):
        h = CallbackHarness()
        first = h.payload()
        original = h.recharges.update_one
        calls = {"n": 0}

        def crash_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db hiccup")
            return original(*args, **kwargs)

        h.recharges.update_one = crash_once
        with self.assertRaises(RuntimeError):
            h.call(first)
        self.assertEqual(h.balance, 7_700)
        self.assertEqual(h.topup["status"], "settling")
        record_processing_error(h.topups, "req-1", "db hiccup", "now")
        self.assertEqual(h.topup["status"], "settling")
        # An automatic retry cannot race a still-running settlement or double-credit.
        self.assertEqual(h.call(first)[0], {"status": "already_processed"})
        self.assertEqual(h.balance, 7_700)
        self.assertEqual(h.recharges.docs, [])
        self.assertEqual(len(h.successes), 0)

    def test_legacy_processing_topup_without_stored_result_adopts_first_result(self):
        h = CallbackHarness()
        h.topups.update_one({"_id": "topup-1"}, {"$set": {"status": "processing"}})
        self.assertEqual(h.call(h.payload())[0], {"status": "ok"})
        self.assertEqual(h.balance, 7_700)


class PayloadParsingTests(unittest.TestCase):
    def test_json_then_form_then_query_precedence(self):
        self.assertEqual(parse_callback_payload({"a": 1}, {"b": "2"}, {"c": "3"}), {"a": 1})
        self.assertEqual(parse_callback_payload(None, {"b": "2"}, {"c": "3"}), {"b": "2"})
        self.assertEqual(parse_callback_payload({}, {}, {"c": "3"}), {"c": "3"})
        self.assertEqual(parse_callback_payload(["not", "dict"], {}, {}), {})

    def test_form_and_query_string_values_credit_correctly(self):
        string_payload = {k: (str(v) if v is not None else v)
                          for k, v in CallbackHarness().payload().items()}
        for source in ("form", "query"):
            with self.subTest(source=source):
                h = CallbackHarness()
                data = (parse_callback_payload(None, string_payload, {}) if source == "form"
                        else parse_callback_payload(None, {}, string_payload))
                self.assertEqual(h.call(data)[0], {"status": "ok"})
                self.assertEqual(h.balance, 7_700)

    def test_both_callback_routes_still_registered(self):
        source = (pathlib.Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.route("/charge/callback", methods=["GET", "POST"])', source)
        self.assertIn('@app.route("/gachthefast-callback", methods=["GET", "POST"])', source)


if __name__ == "__main__":
    unittest.main()
