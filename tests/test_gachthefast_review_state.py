import ast
import pathlib
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace


class UpdateResult:
    def __init__(self, modified_count):
        self.modified_count = modified_count


class FakeCollection:
    def __init__(self, document):
        self.document = document

    def update_one(self, query, update):
        for key, condition in query.items():
            actual = self.document.get(key)
            if isinstance(condition, dict):
                if "$in" in condition and actual not in condition["$in"]:
                    return UpdateResult(0)
                if "$exists" in condition:
                    exists = key in self.document
                    if exists != condition["$exists"]:
                        return UpdateResult(0)
            elif actual != condition:
                return UpdateResult(0)

        self.document.update(update.get("$set", {}))
        return UpdateResult(1)


def load_review_function(document, notifications):
    source_path = pathlib.Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_on_gachthefast_review"
    )
    namespace = {
        "datetime": datetime,
        "timezone": timezone,
        "col_card_topups": FakeCollection(document),
        "notify_admins": lambda **kwargs: notifications.append(kwargs),
        "os": SimpleNamespace(environ={}),
        "print": lambda *args, **kwargs: None,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace["_on_gachthefast_review"]


class ReviewStateTransitionTests(unittest.TestCase):
    def setUp(self):
        self.document = {
            "_id": "topup-1",
            "status": "settling",
            "request_id": "request-1",
            "username": "test-user",
            "telco": "VIETTEL",
        }
        self.notifications = []
        self.review = load_review_function(self.document, self.notifications)

    def test_interrupted_settlement_moves_to_review_atomically_with_outbox(self):
        self.review(self.document, "settlement_interrupted_manual_reconciliation", {
            "face_value": 10000,
            "gachthefast_amount": 8050,
            "credited_amount": 7700,
            "shop_margin": 350,
            "target_discount_pct": 23,
        })

        self.assertEqual(self.document["status"], "review")
        self.assertEqual(self.document["review_reason"], "settlement_interrupted_manual_reconciliation")
        self.assertEqual(self.document["review_delivery"]["status"], "pending")
        self.assertEqual(self.document["review_delivery"]["payload"]["event_id"],
                         "narri-gachthefast-review:topup-1:settlement_interrupted_manual_reconciliation")
        self.assertEqual(len(self.notifications), 1)

    def test_existing_review_state_still_creates_outbox(self):
        self.document["status"] = "review"
        self.review(self.document, "unknown_callback_status", {})

        self.assertEqual(self.document["status"], "review")
        self.assertEqual(self.document["review_delivery"]["status"], "pending")

    def test_duplicate_review_call_does_not_replace_existing_outbox(self):
        self.review(self.document, "settlement_interrupted_manual_reconciliation", {})
        first_event = self.document["review_delivery"]["event_id"]
        self.review(self.document, "settlement_interrupted_manual_reconciliation", {})

        self.assertEqual(self.document["status"], "review")
        self.assertEqual(self.document["review_delivery"]["event_id"], first_event)
        self.assertEqual(len(self.notifications), 1)


if __name__ == "__main__":
    unittest.main()
