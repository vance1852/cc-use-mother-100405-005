import unittest

from science_strategy_foundation.pilot.acceptance import run


class PilotAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual("partial_qualified", result["callback_result"])
        self.assertEqual("tech", result["deviation_responsible"])
        self.assertEqual("in_production", result["recall_preserved_status"])
        self.assertEqual(1, result["cancelled_payment_obligations"])
        self.assertEqual(0, result["remaining_open_obligations"])
        self.assertTrue(result["lineage_authorized"])
        self.assertEqual(1, result["impacted_batch_count"])


if __name__ == "__main__":
    unittest.main()
