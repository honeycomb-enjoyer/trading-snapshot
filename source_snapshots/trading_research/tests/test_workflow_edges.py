import unittest
from unittest.mock import patch

from optimizer.runner import get_varying_params
from overfit_tests.walkforward_test.metrics import compute_parameter_stability
from runners import walk_forward


class WorkflowEdgeTests(unittest.TestCase):
    def test_optimizer_accepts_nested_fixed_strategy_config(self):
        results = [
            {
                "strategy_params": {"window": {"start": ["UTC", "07:00"]}, "filter": value},
                "execution_params": {"mode": "next_bar"},
            }
            for value in (False, True)
        ]
        strategy, execution = get_varying_params(results)
        self.assertEqual(strategy, ["filter"])
        self.assertEqual(execution, [])

    def test_walk_forward_parameter_stability_accepts_nested_session_window(self):
        window = {
            "start": ["Europe/London", "08:00"],
            "boundary": ["America/New_York", "09:30"],
            "end": ["Europe/London", "16:30"],
        }
        results = [
            {"strategy_params": {"session_window": window, "sl_atr": value}}
            for value in (2.0, 2.0, 2.0, 3.0, 3.0)
        ]

        self.assertEqual(compute_parameter_stability(results), 60.0)

    @patch("runners.walk_forward.generate_walkforward_windows", return_value=[])
    @patch("runners.walk_forward.print_windows_summary")
    @patch("runners.walk_forward.select_dataset", return_value=[])
    @patch("runners.walk_forward.prepare_data")
    def test_walk_forward_handles_no_valid_windows_without_traceback(
        self, prepare_data, select_dataset, print_summary, generate_windows
    ):
        self.assertEqual(walk_forward.main(), ([], None))


if __name__ == "__main__":
    unittest.main()
