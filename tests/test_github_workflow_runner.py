import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.run_github_workflow import (
    _parse_github_time,
    load_workflow_config,
    print_preset_list,
    resolve_preset,
    tail_lines,
)


class GitHubWorkflowConfigTests(unittest.TestCase):
    def test_load_workflow_config_from_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "workflows.json"
            path.write_text(
                json.dumps({"repository": "owner/repo", "presets": {}}),
                encoding="utf-8",
            )
            config = load_workflow_config(path)
            self.assertEqual(config["repository"], "owner/repo")

    def test_resolve_preset_normalizes_inputs(self) -> None:
        config = {
            "presets": {
                "publish-private-videos": {
                    "workflow": "publish.yml",
                    "inputs": {"mode": "videos", "timing": "scheduled"},
                }
            }
        }
        preset = resolve_preset(config, "publish-private-videos")
        self.assertEqual(preset["workflow"], "publish.yml")
        self.assertEqual(preset["inputs"], {"mode": "videos", "timing": "scheduled"})

    def test_resolve_preset_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            resolve_preset({"presets": {}}, "missing")

    def test_parse_github_time(self) -> None:
        parsed = _parse_github_time("2026-07-13T06:04:12Z")
        self.assertEqual(parsed, datetime(2026, 7, 13, 6, 4, 12, tzinfo=timezone.utc))

    def test_tail_lines(self) -> None:
        text = "\n".join(f"line {index}" for index in range(10))
        tailed = tail_lines(text, 3)
        self.assertEqual(tailed, "line 7\nline 8\nline 9")

    def test_example_config_has_private_publish_preset(self) -> None:
        example = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "github_workflows.example.json"
        )
        config = load_workflow_config(example)
        preset = resolve_preset(config, "publish-private-videos")
        self.assertEqual(preset["inputs"]["timing"], "scheduled")
        self.assertEqual(preset["workflow"], "publish.yml")

    def test_print_preset_list_includes_cron_reference(self) -> None:
        config = {
            "presets": {"demo": {"workflow": "publish.yml", "inputs": {}}},
            "cron_jobs": {
                "demo-cron": {
                    "schedule": "0 0 * * *",
                    "timezone": "Europe/Sofia",
                    "preset": "demo",
                }
            },
        }
        with patch("builtins.print") as mock_print:
            print_preset_list(config)
        output = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("demo-cron", output)
        self.assertIn("Europe/Sofia", output)


if __name__ == "__main__":
    unittest.main()
