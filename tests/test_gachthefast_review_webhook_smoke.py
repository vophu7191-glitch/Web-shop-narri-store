import ast
import json
import os
import pathlib
import unittest
from unittest.mock import patch


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app.py"


def load_functions(*names):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {"os": os, "json": json}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class FakeResponse:
    status = 200

    def __init__(self, payload=None, status=200):
        self.payload = payload if payload is not None else {"success": True}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return json.dumps(self.payload).encode("utf-8")


class ReviewWebhookSmokeTestTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_functions(
            "_gachthefast_review_test_enabled",
            "_post_gachthefast_review_webhook",
        )

    def test_test_button_requires_explicit_flag_and_exact_staging_endpoint(self):
        with patch.dict(os.environ, {
            "NARRI_REVIEW_TEST_ENABLED": "1",
            "NARRI_REVIEW_WEBHOOK_URL": (
                "https://narri-discord-bot-staging.onrender.com/internal/gachthefast-review"
            ),
        }, clear=False):
            self.assertTrue(self.ns["_gachthefast_review_test_enabled"]())

        with patch.dict(os.environ, {
            "NARRI_REVIEW_TEST_ENABLED": "1",
            "NARRI_REVIEW_WEBHOOK_URL": "https://narri-store.onrender.com/internal/gachthefast-review",
        }, clear=False):
            self.assertFalse(self.ns["_gachthefast_review_test_enabled"]())

    def test_test_button_is_disabled_without_explicit_flag(self):
        with patch.dict(os.environ, {
            "NARRI_REVIEW_TEST_ENABLED": "0",
            "NARRI_REVIEW_WEBHOOK_URL": (
                "https://narri-discord-bot-staging.onrender.com/internal/gachthefast-review"
            ),
        }, clear=False):
            self.assertFalse(self.ns["_gachthefast_review_test_enabled"]())

    def test_post_sends_payload_and_shared_secret_and_requires_positive_ack(self):
        payload = {"event_id": "synthetic-test-1", "reason": "synthetic_webhook_test"}
        with patch.dict(os.environ, {
            "NARRI_REVIEW_WEBHOOK_URL": (
                "https://narri-discord-bot-staging.onrender.com/internal/gachthefast-review"
            ),
            "NARRI_REVIEW_WEBHOOK_SECRET": "unit-test-secret",
        }, clear=False), patch(
            "urllib.request.urlopen", return_value=FakeResponse()
        ) as send:
            result = self.ns["_post_gachthefast_review_webhook"](payload)

        self.assertEqual(result, {"success": True})
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, os.environ["NARRI_REVIEW_WEBHOOK_URL"])
        self.assertEqual(request.get_header("X-narri-review-secret"), "unit-test-secret")
        self.assertEqual(json.loads(request.data.decode("utf-8")), payload)
        self.assertEqual(send.call_args.kwargs["timeout"], 35)

    def test_non_success_ack_is_rejected(self):
        with patch.dict(os.environ, {
            "NARRI_REVIEW_WEBHOOK_URL": "https://example.invalid/internal/gachthefast-review",
            "NARRI_REVIEW_WEBHOOK_SECRET": "unit-test-secret",
        }, clear=False), patch(
            "urllib.request.urlopen", return_value=FakeResponse({"success": False})
        ):
            with self.assertRaisesRegex(RuntimeError, "bot_ack_not_confirmed"):
                self.ns["_post_gachthefast_review_webhook"]({"event_id": "x"})

    def test_missing_webhook_configuration_fails_before_network(self):
        with patch.dict(os.environ, {
            "NARRI_REVIEW_WEBHOOK_URL": "",
            "NARRI_REVIEW_WEBHOOK_SECRET": "",
        }, clear=False), patch("urllib.request.urlopen") as send:
            with self.assertRaisesRegex(RuntimeError, "webhook_not_configured"):
                self.ns["_post_gachthefast_review_webhook"]({"event_id": "x"})
        send.assert_not_called()

    def test_admin_route_is_admin_csrf_protected_and_does_not_touch_wallet_or_topups(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        route = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "admin_test_gachthefast_review_webhook"
        )
        decorators = {
            item.func.id if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
            else item.id if isinstance(item, ast.Name) else ""
            for item in route.decorator_list
        }
        self.assertIn("admin_required", decorators)
        self.assertIn("csrf_protect", decorators)
        calls = {
            node.func.id for node in ast.walk(route)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("_gachthefast_review_test_enabled", calls)
        self.assertIn("_post_gachthefast_review_webhook", calls)
        forbidden = {"credit_user_once", "col_card_topups", "col_users", "col_recharges"}
        self.assertTrue(forbidden.isdisjoint(calls))


if __name__ == "__main__":
    unittest.main()
