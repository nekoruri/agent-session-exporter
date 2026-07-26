from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent_session_exporter.core import (
    CollectorConfig,
    Config,
    EventStore,
    ServerConfig,
)
from agent_session_exporter.server import _handler


class CollectorTest(unittest.TestCase):
    def test_authenticated_hook_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                vault_path=None,
                destination="archive",
                state_dir=root / "state",
                device_id="collector",
                redact=True,
                include_tool_details=False,
                sync_on_capture=True,
                project_aliases={},
                collector=CollectorConfig(),
                server=ServerConfig(),
            )
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                _handler(config, "test-token"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                body = json.dumps(
                    {
                        "session_id": "cloud-session",
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Hello cloud",
                    }
                ).encode("utf-8")
                unauthorized = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/hooks/claude-cloud",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(unauthorized, timeout=2)
                self.assertEqual(context.exception.code, 401)

                authorized = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/hooks/claude-cloud",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer test-token",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(authorized, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                with EventStore(config.state_dir) as store:
                    events = store.list_events()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].source, "claude-cloud")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_storage_failure_returns_json_service_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                vault_path=None,
                destination="archive",
                state_dir=root / "state",
                device_id="collector",
                redact=True,
                include_tool_details=False,
                sync_on_capture=True,
                project_aliases={},
                collector=CollectorConfig(),
                server=ServerConfig(),
            )
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                _handler(config, "test-token"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                body = json.dumps(
                    {
                        "session_id": "cloud-session",
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Hello cloud",
                    }
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/hooks/claude-cloud",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer test-token",
                    },
                    method="POST",
                )
                with (
                    patch(
                        "agent_session_exporter.server.EventStore.add_event",
                        side_effect=sqlite3.OperationalError("database is locked"),
                    ),
                    self.assertRaises(urllib.error.HTTPError) as context,
                ):
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(context.exception.code, 503)
                response = json.loads(context.exception.read())
                self.assertEqual(response, {"error": "storage unavailable"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
