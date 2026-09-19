from __future__ import annotations

import http.client
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from archotraz.console import make_handler, render_dashboard, state_payload  # type: ignore  # noqa: E402
from archotraz.core import Warden  # type: ignore  # noqa: E402


class ConsoleTests(unittest.TestCase):
    def test_dashboard_is_local_operations_surface_and_marks_disabled_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            warden = Warden(Path(td))
            try:
                html = render_dashboard(warden)
                self.assertIn("ARCHOTRAZ", html)
                self.assertIn("Upload snapshot", html)
                self.assertIn("Untrusted execution: disabled", html)
                self.assertIn("Automatic scoring: disabled", html)
                self.assertIn("Automatic cell assignment: disabled", html)
                state = state_payload(warden)
                self.assertEqual(state["capabilities"]["untrusted_execution"], "disabled")
                self.assertEqual(state["capabilities"]["automatic_scoring"], "disabled")
            finally:
                warden.close()

    def test_candidate_forms_carry_version_and_stable_idempotency_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            warden = Warden(Path(td))
            try:
                result = warden.ingest_snapshot(
                    source_uri="manual://forms",
                    snapshot_bytes=b"not a zip",
                    filename="forms.bin",
                    description="forms",
                    idempotency_key="forms-ingest",
                )
                html = render_dashboard(warden)
                candidate = warden.get_candidate(result["candidate_id"])
                self.assertIn(f'name="expected_version" value="{candidate["version"]}"', html)
                self.assertIn('value="web-exclude-', html)
                self.assertIn('value="web-cell-', html)
            finally:
                warden.close()

    def test_mutating_route_rejects_attacker_controlled_host_even_with_matching_origin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            warden = Warden(Path(td))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(warden))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                hostile_host = f"evil.example:{server.server_port}"
                connection.request(
                    "POST",
                    "/matches",
                    body="idempotency_key=host-test",
                    headers={
                        "Host": hostile_host,
                        "Origin": f"http://{hostile_host}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 400)
                self.assertIn("local Host", body)
            finally:
                server.shutdown()
                server.server_close()
                warden.close()


if __name__ == "__main__":
    unittest.main()
