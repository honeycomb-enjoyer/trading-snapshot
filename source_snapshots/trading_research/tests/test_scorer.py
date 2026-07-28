import unittest

from optimizer.scorer import is_valid_result


class ScorerConfigTests(unittest.TestCase):
    def test_workflow_can_override_strategy_specific_acceptance_gates(self):
        metrics = {
            "total_trades": 50,
            "profit_factor": 0.9,
            "net_r": -2.0,
            "max_drawdown": 12.0,
        }
        self.assertFalse(is_valid_result(metrics))
        self.assertTrue(is_valid_result(metrics, {
            "min_trades": 20,
            "min_profit_factor": None,
            "min_net_r": None,
            "max_drawdown": None,
        }))


if __name__ == "__main__":
    unittest.main()
