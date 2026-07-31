from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_session_exporter.codex_cloud import sync_codex_cloud
from agent_session_exporter.core import ClaudeCloudConfig, Config, EventStore


def config_for(root: Path) -> Config:
    return Config(
        vault_path=None,
        destination="archive",
        state_dir=root / "state",
        device_id="test-device",
        redact=True,
        include_tool_details=False,
        sync_on_capture=True,
        project_aliases={},
        claude_cloud=ClaudeCloudConfig(),
    )


class CodexCloudTest(unittest.TestCase):
    def test_sync_codex_cloud_parses_and_stores_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            output = json.dumps(
                [
                    {
                        "id": "task-1",
                        "repository": "example/project",
                        "branch": "main",
                        "updated_at": "2026-07-26T00:00:00Z",
                    }
                ]
            )
            with patch(
                "agent_session_exporter.codex_cloud._run_codex_cloud",
                return_value=output,
            ) as run_cloud:
                self.assertEqual(sync_codex_cloud(config, limit=1), 1)

            run_cloud.assert_called_once_with(["list", "--json", "--limit", "1"])
            with EventStore(config.state_dir) as store:
                events = store.list_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].source, "codex-cloud")
            self.assertEqual(events[0].project, "project")


if __name__ == "__main__":
    unittest.main()
