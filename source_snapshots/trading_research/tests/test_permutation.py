import unittest

import numpy as np
import pandas as pd

from overfit_tests.permutation_test.permutator import get_permutation
from overfit_tests.permutation_test.permutation_test_runner import permutation_verdict


class PermutationTests(unittest.TestCase):
    def test_seed_is_reproducible_without_mutating_global_rng(self):
        index = pd.date_range("2024-01-01", periods=20, freq="h", tz="UTC")
        base = np.linspace(10.0, 11.0, len(index))
        frame = pd.DataFrame({
            "open": base,
            "high": base + 0.2,
            "low": base - 0.2,
            "close": base + 0.1,
        }, index=index)
        np.random.seed(123)
        expected_next = np.random.random()
        np.random.seed(123)
        first = get_permutation(frame, seed=7)
        actual_next = np.random.random()
        second = get_permutation(frame, seed=7)
        pd.testing.assert_frame_equal(first, second)
        self.assertEqual(actual_next, expected_next)

    def test_verdict_uses_corrected_p_value_not_small_sample_percentile(self):
        self.assertEqual(permutation_verdict(1 / 3), "UNCERTAIN")
        self.assertEqual(permutation_verdict(0.01), "EXTREMELY STRONG EDGE")


if __name__ == "__main__":
    unittest.main()
