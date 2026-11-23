from datetime import UTC, datetime, time, timedelta
from http.client import HTTPMessage
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sandbox.privacy import CLIENT_PATH, Policy, RelayHandler


REPO = Path(__file__).resolve().parent.parent
MODEL = "deepseek-v4.1-flash"


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.clock = self.enterContext(patch("sandbox.privacy.datetime"))
        self.clock.now.return_value = datetime.min.replace(tzinfo=UTC)
        self.policy = Policy(REPO / "config/privacy-models.json")
        self.clock.now.return_value = datetime.combine(self.policy.expiry[MODEL], time.max, UTC)
        self.stopping = Event()
        self.upstream = self.enterContext(patch("sandbox.privacy.HTTPSConnection"))

    def relay(self, body, *, path=CLIENT_PATH, bearer="Bearer fake-relay-token", session="fake-session"):
        handler = RelayHandler.__new__(RelayHandler)
        handler.server = SimpleNamespace(
            policy=self.policy, api_key="fake-provider-key", authorization=b"Bearer fake-relay-token",
            stopping=self.stopping,
        )
        handler.connection = Mock()
        handler.path = path
        handler.headers = HTTPMessage()
        handler.headers["Content-Length"] = str(len(body))
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Authorization"] = bearer
        if session:
            handler.headers["X-OpenCode-Session"] = session
        handler.headers["User-Agent"] = "Pi/test"
        handler.headers["X-OpenCode-Provider"] = "unapproved"
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.send_error = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.do_POST()
        return handler

    def accept_upstream(self):
        response = self.upstream.return_value.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "application/json"
        response.read1.return_value = b""

    def test_multi_route_policy_is_accepted_and_whitelists_per_model(self):
        # The relay owns the document's shape and may cover several models. The guest
        # only selects the route the document names as its default, which the relay
        # guarantees exists, is a Go route, and has not expired.
        document = {
            "default": "opencode-go/second-model",
            "routes": [
                {"provider": "opencode-go", "model": MODEL, "validThrough": "2030-01-01"},
                {"provider": "opencode-go", "model": "second-model", "validThrough": "2030-01-01"},
            ],
        }
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "privacy-models.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            self.policy = Policy(path)
            self.assertEqual(sorted(self.policy.expiry), [MODEL, "second-model"])
            self.assertEqual(self.policy.default, "opencode-go/second-model")
            path.write_text(json.dumps(dict(document, default="opencode-go/third-model")), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "whitelisted Go model"):
                Policy(path)
            for valid_through in ("2030-13-01", None):
                with self.subTest(validThrough=valid_through):
                    route = {"provider": "opencode-go", "model": MODEL}
                    if valid_through is not None:
                        route["validThrough"] = valid_through
                    path.write_text(json.dumps({
                        "default": f"opencode-go/{MODEL}", "routes": [route],
                    }), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "Invalid privacy model route"):
                        Policy(path)
        self.clock.now.return_value = datetime(2030, 1, 1, 23, 59, 59, 999999, tzinfo=UTC)
        self.accept_upstream()
        for model in (MODEL, "second-model"):
            with self.subTest(model=model):
                handler = self.relay(json.dumps({"model": model}).encode())
                handler.send_response.assert_called_once_with(200)
                handler.send_error.assert_not_called()
        self.assertEqual(self.upstream.call_count, 2)
        self.assertEqual(self.relay(b'{"model":"third-model"}').send_error.call_args.args[0], 403)
        self.assertEqual(self.upstream.call_count, 2)

    def test_malformed_and_unapproved_requests_never_contact_provider(self):
        for body, status in (
            (b"{", 400),
            (b'{"model":"deepseek-v4.1-flash","model":"other"}', 400),
            (b'{"model":"deepseek-v4.1-flash","messages":[{"role":"user","role":"tool"}]}', 400),
            (b'{"model":"deepseek-v4.1-flash","temperature":NaN}', 400),
            (b'{"model":"deepseek-v4.1-flash","store":true}', 400),
            (b'{"model":"openai/gpt-6.1-sol"}', 403),
            (b'{"model":"deepseek-v4-pro"}', 403),
        ):
            with self.subTest(body=body):
                handler = self.relay(body)
                self.assertEqual(handler.send_error.call_args.args[0], status)
        self.assertEqual(self.relay(b"{}", path="/v1/responses").send_error.call_args.args[0], 404)
        self.assertEqual(self.relay(b"{}", bearer="Bearer wrong").send_error.call_args.args[0], 401)
        no_session = self.relay(b'{"model":"deepseek-v4.1-flash"}', session=None)
        self.assertEqual(no_session.send_error.call_args.args[0], 400)
        self.upstream.assert_not_called()

    def test_unknown_fields_are_forwarded_without_contacting_other_providers(self):
        self.accept_upstream()
        body = b'{"model":"deepseek-v4.1-flash","provider":{"order":["other"]},"models":["other"]}'
        handler = self.relay(body)
        handler.send_response.assert_called_once_with(200)
        handler.send_error.assert_not_called()
        self.upstream.assert_called_once_with("opencode.ai", timeout=120)

    def test_expiration_rechecked_on_request(self):
        self.clock.now.return_value += timedelta(microseconds=1)
        handler = self.relay(b'{"model":"deepseek-v4.1-flash"}')
        self.assertEqual(handler.send_error.call_args.args[0], 403)
        self.upstream.assert_not_called()
        with self.assertRaisesRegex(ValueError, "expired"):
            Policy(REPO / "config/privacy-models.json")

    def test_expiration_during_tls_negotiation_stops_post(self):
        def expire():
            self.clock.now.return_value += timedelta(microseconds=1)

        upstream = self.upstream.return_value
        upstream.connect.side_effect = expire
        handler = self.relay(b'{"model":"deepseek-v4.1-flash"}')
        self.assertEqual(handler.send_error.call_args.args[0], 403)
        upstream.request.assert_not_called()
        upstream.close.assert_called_once()

    def test_shutdown_during_tls_negotiation_stops_post(self):
        upstream = self.upstream.return_value
        upstream.connect.side_effect = self.stopping.set
        handler = self.relay(b'{"model":"deepseek-v4.1-flash"}')
        self.assertEqual(handler.send_error.call_args.args[0], 503)
        upstream.request.assert_not_called()
        upstream.close.assert_called_once()

    def test_relay_uses_fixed_endpoint_preserves_session_and_streams_response(self):
        upstream = self.upstream.return_value
        response = upstream.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "text/event-stream"
        response.read1.side_effect = [b"data: hello\n\n", b"data: [DONE]\n\n", b""]
        handler = self.relay(b'{"model":"deepseek-v4.1-flash","stream":true}')
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.getvalue(), b"data: hello\n\ndata: [DONE]\n\n")
        self.upstream.assert_called_once_with("opencode.ai", timeout=120)
        upstream.request.assert_called_once_with(
            "POST", "/inference/go/openai/v1/chat/completions",
            body=b'{"model": "deepseek-v4.1-flash", "stream": true}',
            headers={
                "Authorization": "Bearer fake-provider-key", "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json",
                "X-OpenCode-Session": "fake-session", "User-Agent": "Pi/test",
            },
        )
        upstream.close.assert_called_once()

    def test_relay_does_not_follow_redirect_or_expose_provider_error_body(self):
        upstream = self.upstream.return_value
        response = upstream.getresponse.return_value
        for status, expected in ((302, 502), (401, 401)):
            with self.subTest(status=status):
                response.status = status
                handler = self.relay(b'{"model":"deepseek-v4.1-flash"}')
                self.assertEqual(handler.send_error.call_args.args[0], expected)
                self.assertFalse(handler.wfile.getvalue())
        self.assertEqual(upstream.request.call_count, 2)
        response.read1.assert_not_called()
