import os
import unittest


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SYNC_WORKFLOW_PATH = os.path.join(ROOT_DIR, ".github", "workflows", "sync.yml")


class WorkflowSecretRotationTests(unittest.TestCase):
    def test_piped_secret_values_are_read_from_stdin(self) -> None:
        with open(SYNC_WORKFLOW_PATH, "r", encoding="utf-8") as handle:
            workflow = handle.read()

        self.assertNotIn("--body -", workflow)
        self.assertIn("| GH_TOKEN=", workflow)
        self.assertIn("gh secret set STRAVA_REFRESH_TOKEN", workflow)
        self.assertIn("gh secret set GARMIN_TOKENS_B64", workflow)


if __name__ == "__main__":
    unittest.main()
