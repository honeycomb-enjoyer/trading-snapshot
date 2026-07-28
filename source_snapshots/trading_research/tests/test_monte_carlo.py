import random
import unittest

from overfit_tests.monte_carlo_test.simulator import run_shuffle_mc, run_synthetic_mc


class MonteCarloTests(unittest.TestCase):
    def test_seed_is_reproducible_without_mutating_global_rng(self):
        trades = [{"R": value} for value in (1.0, -1.0, 2.0, -0.5)]
        random.seed(123)
        expected_next = random.random()
        random.seed(123)
        first = run_shuffle_mc(trades, simulations=3, seed=7)
        actual_next = random.random()
        second = run_shuffle_mc(trades, simulations=3, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(actual_next, expected_next)

        synthetic_one = run_synthetic_mc(0.4, 0.1, 2.0, -1.0, 10, simulations=3, seed=9)
        synthetic_two = run_synthetic_mc(0.4, 0.1, 2.0, -1.0, 10, simulations=3, seed=9)
        self.assertEqual(synthetic_one, synthetic_two)


if __name__ == "__main__":
    unittest.main()
