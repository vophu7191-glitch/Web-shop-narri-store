import ast
import json
import pathlib
import time
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


class UpdateResult:
    def __init__(self, modified_count):
        self.modified_count = modified_count


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
    target[parts[-1]] = value


class FakeCollection:
    """Minimal atomic Mongo update subset used by the real outbox function."""

    def __init__(self, document):
        self.document = document

    def _matches_condition(self, actual, condition):
        if isinstance(condition, dict):
            for operator, expected in condition.items():
                if operator == "$lte" and not (actual is not None and actual <= expected):
                    return False
                if operator == "$exists":
                    exists = actual is not None
                    if exists != expected:
                        return False
            return True
        return actual == condition

    def _matches(self, query):
        for key, condition in query.items():
            if key == "$or":
                if not any(self._matches(item) for item in condition):
                    return False
                continue
            if not self._matches_condition(_get_path(self.document, key), condition):
                return False
        return True

    def update_one(self, query, update):
        if not self._matches(query):
            return UpdateResult(0)
        for key, value in update.get("$set", {}).items():
            _set_path(self.document, key, value)
        for key, value in update.get("$inc", {}).items():
            current = _get_path(self.document, key) or 0
            _set_path(self.document, key, current + value)
        return UpdateResult(1)

    def find_one(self, query):
        return self.document.copy() if self._matches(query) else None


def load_real_delivery_function():
    source_path = pathlib.Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {
            "_deliver_gachthefast_review_outbox",
            "_post_gachthefast_review_webhook",
        }
    ]
    module = ast.Module(body=functions, type_ignores=[])
    namespace = {
        "datetime": datetime,
        "timezone": timezone,
        "timedelta": timedelta,
        "json": json,
        "os": __import__("os"),
        "time": time,
        "print": lambda *args, **kwargs: None,
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace


class FakeResponse:
    status = 200

    def __init__(self, payload=None):
        self.payload = payload or {"success": True}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return json.dumps(self.payload).encode("utf-8")


class ReviewOutboxDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.document = {
            "_id": "topup-1",
            "review_delivery": {
                "event_id": "event-1",
                "status": "pending",
                "payload": {"event_id": "event-1"},
                "attempts": 0,
                "created_at": self.now,
            },
        }
        self.collection = FakeCollection(self.document)
        self.namespace = load_real_delivery_function()
        self.namespace["col_card_topups"] = self.collection
        self.namespace["os"].environ["NARRI_REVIEW_WEBHOOK_URL"] = "https://bot.invalid/review"
        self.namespace["os"].environ["NARRI_REVIEW_WEBHOOK_SECRET"] = "test-secret"

    def tearDown(self):
        self.namespace["os"].environ.pop("NARRI_REVIEW_WEBHOOK_URL", None)
        self.namespace["os"].environ.pop("NARRI_REVIEW_WEBHOOK_SECRET", None)

    def deliver(self):
        return self.namespace["_deliver_gachthefast_review_outbox"]("topup-1")

    def test_success_ack_marks_event_sent(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            self.assertTrue(self.deliver())
        delivery = self.document["review_delivery"]
        self.assertEqual(delivery["status"], "sent")
        self.assertEqual(delivery["attempts"], 1)
        self.assertIsNone(delivery["lease_until"])
        self.assertIsNone(delivery["last_error"])

    def test_missing_success_ack_keeps_event_pending_for_retry(self):
        response = FakeResponse({"success": False})
        with patch("urllib.request.urlopen", return_value=response) as send, patch("time.sleep"):
            self.assertFalse(self.deliver())
        delivery = self.document["review_delivery"]
        self.assertEqual(send.call_count, 3)
        self.assertEqual(delivery["status"], "pending")
        self.assertIn("bot_ack_not_confirmed", delivery["last_error"])

    def test_network_failure_keeps_event_pending_for_retry(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("network timeout")) as send, patch("time.sleep"):
            self.assertFalse(self.deliver())
        delivery = self.document["review_delivery"]
        self.assertEqual(send.call_count, 3)
        self.assertEqual(delivery["status"], "pending")
        self.assertIn("network timeout", delivery["last_error"])

    def test_active_sending_lease_prevents_duplicate_claim(self):
        delivery = self.document["review_delivery"]
        delivery["status"] = "sending"
        delivery["lease_until"] = self.now + timedelta(minutes=1)
        with patch("urllib.request.urlopen") as send:
            self.assertFalse(self.deliver())
        send.assert_not_called()
        self.assertEqual(delivery["attempts"], 0)

    def test_expired_sending_lease_is_recovered(self):
        delivery = self.document["review_delivery"]
        delivery["status"] = "sending"
        delivery["lease_until"] = self.now - timedelta(seconds=1)
        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            self.assertTrue(self.deliver())
        self.assertEqual(delivery["status"], "sent")
        self.assertEqual(delivery["attempts"], 1)


    def test_failed_delivery_is_retried_with_same_event_and_then_marked_sent(self):
        original_event_id = self.document["review_delivery"]["event_id"]
        original_payload = self.document["review_delivery"]["payload"]

        with patch(
            "urllib.request.urlopen",
            side_effect=TimeoutError("temporary network failure"),
        ) as failed_send, patch("time.sleep"):
            self.assertFalse(self.deliver())

        self.assertEqual(failed_send.call_count, 3)
        self.assertEqual(self.document["review_delivery"]["status"], "pending")
        self.assertEqual(self.document["review_delivery"]["attempts"], 1)

        with patch(
            "urllib.request.urlopen",
            return_value=FakeResponse({"success": True}),
        ) as recovered_send:
            self.assertTrue(self.deliver())

        self.assertEqual(recovered_send.call_count, 1)
        delivery = self.document["review_delivery"]
        self.assertEqual(delivery["status"], "sent")
        self.assertEqual(delivery["attempts"], 2)
        self.assertEqual(delivery["event_id"], original_event_id)
        self.assertEqual(delivery["payload"], original_payload)




class MonitorCursor:
    def __init__(self, rows):
        self.rows = rows

    def limit(self, count):
        return self.rows[:count]


def load_review_monitor():
    source_path = pathlib.Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_stale_gachthefast_settlement_monitor"
    )
    namespace = {
        "datetime": datetime,
        "timezone": timezone,
        "timedelta": timedelta,
        "time": SimpleNamespace(sleep=lambda seconds: (_ for _ in ()).throw(StopMonitor())),
        "print": lambda *args, **kwargs: None,
        "_on_gachthefast_review": lambda *args, **kwargs: None,
        "_deliver_gachthefast_review_outbox": lambda topup_id: None,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


class StopMonitor(BaseException):
    pass


class ReviewOutboxMonitorTests(unittest.TestCase):
    def run_one_monitor_pass(self, namespace):
        with self.assertRaises(StopMonitor):
            namespace["_stale_gachthefast_settlement_monitor"]()

    def test_monitor_retries_pending_and_expired_lease_events(self):
        rows = [{"_id": "pending-1"}, {"_id": "expired-lease-1"}]

        class Collection:
            def find(self, query):
                if "$or" in query:
                    return MonitorCursor(rows)
                return MonitorCursor([])

        namespace = load_review_monitor()
        namespace["col_card_topups"] = Collection()
        delivered = []
        namespace["_deliver_gachthefast_review_outbox"] = delivered.append

        self.run_one_monitor_pass(namespace)

        self.assertEqual(delivered, ["pending-1", "expired-lease-1"])

    def test_monitor_rebuilds_missing_outbox_for_review_rows(self):
        review = {"_id": "review-1", "status": "review", "review_reason": "manual"}
        rebuilt = []

        class Collection:
            def find(self, query):
                if query.get("status") == "review":
                    return MonitorCursor([review])
                return MonitorCursor([])

        namespace = load_review_monitor()
        namespace["col_card_topups"] = Collection()
        namespace["_on_gachthefast_review"] = lambda *args: rebuilt.append(args)

        self.run_one_monitor_pass(namespace)

        self.assertEqual(len(rebuilt), 1)
        self.assertEqual(rebuilt[0][0]["_id"], "review-1")
        self.assertEqual(rebuilt[0][1], "manual")


if __name__ == "__main__":
    unittest.main()
