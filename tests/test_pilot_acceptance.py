"""验收脚本包装测试。"""

import unittest

from pilot_governance.acceptance import run


class PilotAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(all(result["checks"].values()))
        self.assertIn("batch-lab-02", result["affected_batch_ids"])
        self.assertIn("batch-small-01", result["affected_batch_ids"])


if __name__ == "__main__":
    unittest.main()
