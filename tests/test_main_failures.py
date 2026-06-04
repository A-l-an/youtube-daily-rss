from __future__ import annotations

import argparse
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import main as main_module


class MainFailurePolicyTests(unittest.TestCase):
    def test_all_channel_fetch_failures_keep_existing_outputs_and_succeed(self) -> None:
        args = argparse.Namespace(dry_run=False, force=False)

        with (
            patch.object(main_module, "fetch_latest_video", side_effect=RuntimeError("feed unavailable")) as fetch_mock,
            patch.object(main_module, "write_rss") as write_rss_mock,
            patch.object(main_module, "save_json") as save_json_mock,
        ):
            with self.assertLogs(level="ERROR") as logs:
                exit_code = main_module.run(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertIn("keeping existing RSS", "\n".join(logs.output))
        write_rss_mock.assert_not_called()
        save_json_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
