"""Regression tests for the existing SePay webhook; no live provider or database calls."""
import ast
import pathlib
import re
import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app.py"


class FakeRequest:
    def __init__(self, payload, authorization="Apikey test-key"):
        self.payload = payload
        self.headers = {"Authorization": authorization}

    def get_json(self, silent=False):
        return self.payload


class FakeUsers:
    def __init__(self):
        self.docs = {"USER12345678": {
            "_id": "USER12345678", "username": "tester",
            "balance": 1000, "processed_tx_ids": [],
        }}

    def find_one(self, query):
        doc = self.docs.get(query.get("_id"))
        if not doc:
            return None
        if "processed_tx_ids" in query and query["processed_tx_ids"] not in doc.get("processed_tx_ids", []):
            return None
        return dict(doc)

    def find_one_and_update(self, query, update, return_document=None):
        doc = self.docs.get(query.get("_id"))
        if not doc:
            return None
        doc["balance"] += update.get("$inc", {}).get("balance", 0)
        for key, value in update.get("$push", {}).items():
            doc.setdefault(key, []).append(value)
        return dict(doc)


class FakeRecharges:
    def __init__(self):
        self.rows = []

    def insert_one(self, row):
        self.rows.append(dict(row))


def load_webhook():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "sepay_webhook"
    )
    namespace = {
        "request": None,
        "jsonify": lambda value: value,
        "get_config": lambda: {"sepay_api_key": "test-key", "referral_on_recharge": False},
        "re": re,
        "col_users": None,
        "ReturnDocument": SimpleNamespace(AFTER=object()),
        "col_recharges": None,
        "uuid": uuid,
        "datetime": datetime,
        "timezone": timezone,
        "_award_referral_commission": lambda *args: None,
        "notify_admins": lambda **kwargs: None,
        "url_for": lambda name: "/" + name,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class SePayWebhookRegressionTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_webhook()
        self.users = FakeUsers()
        self.recharges = FakeRecharges()
        self.ns["col_users"] = self.users
        self.ns["col_recharges"] = self.recharges
        self.payload = {
            "transferType": "in",
            "transferAmount": 25000,
            "content": "NAPUUSER12345678",
            "id": "sepay-tx-001",
            "gateway": "Test Bank",
        }

    def call_webhook(self, payload=None, authorization="Apikey test-key"):
        self.ns["request"] = FakeRequest(
            self.payload if payload is None else payload, authorization
        )
        return self.ns["sepay_webhook"]()

    def test_valid_incoming_transfer_credits_once_and_records_recharge(self):
        result = self.call_webhook()
        self.assertEqual(result, {"success": True})
        self.assertEqual(self.users.docs["USER12345678"]["balance"], 26000)
        self.assertEqual(self.users.docs["USER12345678"]["processed_tx_ids"], ["sepay-tx-001"])
        self.assertEqual(len(self.recharges.rows), 1)
        self.assertEqual(self.recharges.rows[0]["amount"], 25000)
        self.assertEqual(self.recharges.rows[0]["source"], "sepay")

    def test_duplicate_transaction_does_not_credit_or_record_again(self):
        self.users.docs["USER12345678"]["processed_tx_ids"].append("sepay-tx-001")
        result = self.call_webhook()
        self.assertEqual(result, {"success": True})
        self.assertEqual(self.users.docs["USER12345678"]["balance"], 1000)
        self.assertEqual(self.recharges.rows, [])

    def test_invalid_api_key_is_rejected_before_wallet_update(self):
        result, status = self.call_webhook(authorization="Apikey wrong")
        self.assertEqual(status, 401)
        self.assertFalse(result["success"])
        self.assertEqual(self.users.docs["USER12345678"]["balance"], 1000)
        self.assertEqual(self.recharges.rows, [])

    def test_outgoing_transfer_is_ignored(self):
        payload = dict(self.payload, transferType="out")
        self.assertEqual(self.call_webhook(payload), {"success": True})
        self.assertEqual(self.users.docs["USER12345678"]["balance"], 1000)
        self.assertEqual(self.recharges.rows, [])

    def test_unmatched_reference_is_ignored(self):
        payload = dict(self.payload, content="NO-MATCHING-CODE")
        self.assertEqual(self.call_webhook(payload), {"success": True})
        self.assertEqual(self.users.docs["USER12345678"]["balance"], 1000)
        self.assertEqual(self.recharges.rows, [])


if __name__ == "__main__":
    unittest.main()
