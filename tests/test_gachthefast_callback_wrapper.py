import ast
import copy
import os
import pathlib
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from flask import Flask, jsonify

from gachthefast_logic import credit_user_once, record_processing_error


def _get_path(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _set_path(document, path, value):
    parts = path.split(".")
    target = document
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = copy.deepcopy(value)


class FakeUpdateResult:
    def __init__(self, modified_count):
        self.modified_count = modified_count


class FakeUsers:
    def __init__(self):
        self.document = {
            "_id": "user-route-test",
            "balance": 0,
            "processed_card_topup_ids": [],
        }

    def update_one(self, query, update):
        if query.get("_id") != self.document["_id"]:
            return FakeUpdateResult(0)
        marker = query.get("processed_card_topup_ids", {}).get("$ne")
        if marker in self.document["processed_card_topup_ids"]:
            return FakeUpdateResult(0)
        self.document["balance"] += update.get("$inc", {}).get("balance", 0)
        add_marker = update.get("$addToSet", {}).get("processed_card_topup_ids")
        if add_marker is not None and add_marker not in self.document["processed_card_topup_ids"]:
            self.document["processed_card_topup_ids"].append(add_marker)
        return FakeUpdateResult(1)

    def find_one(self, query, projection=None):
        if query.get("_id") != self.document["_id"]:
            return None
        return copy.deepcopy(self.document)


class FakeTopups:
    """Small atomic single-document Mongo subset for the real callback wrapper."""

    def __init__(self, document):
        self.document = copy.deepcopy(document)

    @staticmethod
    def _matches_condition(actual, condition, exists):
        if isinstance(condition, dict):
            for operator, expected in condition.items():
                if operator == "$in" and actual not in expected:
                    return False
                if operator == "$exists" and exists != expected:
                    return False
            return True
        return actual == condition

    def _matches(self, query):
        for key, condition in query.items():
            actual = _get_path(self.document, key)
            exists = actual is not None
            if not self._matches_condition(actual, condition, exists):
                return False
        return True

    def update_one(self, query, update):
        if not self._matches(query):
            return FakeUpdateResult(0)
        before = copy.deepcopy(self.document)
        for key, value in update.get("$set", {}).items():
            _set_path(self.document, key, value)
        return FakeUpdateResult(int(before != self.document))

    def find_one(self, query):
        return copy.deepcopy(self.document) if self._matches(query) else None


def _load_callback_route():
    source_path = pathlib.Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    route_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "gachthefast_callback"
    )
    route_node.decorator_list = []

    review_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_on_gachthefast_review"
    )
    review_node.decorator_list = []

    notifications = []
    topup = {
        "_id": "topup-route-test",
        "request_id": "request-route-test",
        "username": "test-user",
        "telco": "VIETTEL",
        "declared_value": 10_000,
        "face_value": 10_000,
        "gachthefast_amount": 8_050,
        "credited_amount": 7_700,
        "shop_margin": 350,
        "target_discount_pct": 23,
        "status": "processing",
        "settlement_result": {"value": 10_000, "amount": 8_050},
    }
    topups = FakeTopups(topup)
    namespace = {
        "jsonify": jsonify,
        "datetime": datetime,
        "timezone": timezone,
        "traceback": __import__("traceback"),
        "col_card_topups": topups,
        "record_gachthefast_processing_error": record_processing_error,
        "_read_gachthefast_callback_data": lambda: {"request_id": "request-route-test"},
        "notify_admins": lambda **kwargs: notifications.append(kwargs),
        "os": SimpleNamespace(environ={}),
        "threading": __import__("threading"),
        "print": lambda *args, **kwargs: None,
    }
    module = ast.Module(body=[review_node, route_node], type_ignores=[])
    exec(compile(module, str(source_path), "exec"), namespace)

    flask_app = Flask(__name__)
    flask_app.add_url_rule(
        "/charge/callback", view_func=namespace["gachthefast_callback"], methods=["POST"]
    )
    return flask_app, topups, notifications


class CallbackWrapperExceptionTests(unittest.TestCase):
    def test_route_exception_moves_settling_topup_to_review_with_one_outbox(self):
        flask_app, topups, notifications = _load_callback_route()
        users = FakeUsers()
        calls = {"count": 0}

        def fail_during_settlement():
            calls["count"] += 1
            if calls["count"] == 1:
                topups.document["status"] = "settling"
                # Model a crash after the idempotent wallet write but before
                # the callback can finish its remaining settlement work.
                credit_user_once(users, "user-route-test", "topup-route-test", 7_700)
            raise RuntimeError("simulated settlement operation failure")

        namespace = flask_app.view_functions["gachthefast_callback"].__globals__
        namespace["_gachthefast_callback_impl"] = fail_during_settlement

        client = flask_app.test_client()
        first = client.post("/charge/callback")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json(), {"status": "error"})
        self.assertEqual(topups.document["status"], "review")
        self.assertEqual(
            topups.document["review_reason"],
            "settlement_interrupted_manual_reconciliation",
        )
        self.assertEqual(topups.document["review_delivery"]["status"], "pending")
        self.assertEqual(topups.document["review_delivery"]["attempts"], 0)
        first_event_id = topups.document["review_delivery"]["event_id"]
        self.assertEqual(len(notifications), 1)

        # A repeated exception after the row is already in review must not
        # recreate the durable event or alter the wallet.
        second = client.post("/charge/callback")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json(), {"status": "error"})
        self.assertEqual(topups.document["status"], "review")
        self.assertEqual(topups.document["review_delivery"]["event_id"], first_event_id)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(users.document["balance"], 7_700)
        self.assertEqual(users.document["processed_card_topup_ids"], ["topup-route-test"])


if __name__ == "__main__":
    unittest.main()
