import unittest

from src.intervals import available, normalize


class IntervalTests(unittest.TestCase):
    def test_normalize_sorts_and_merges_overlap(self):
        self.assertEqual([(1, 7), (10, 12)], normalize([(10, 12), (1, 4), (3, 7)]))

    def test_available_returns_inclusive_gaps(self):
        self.assertEqual([(1, 2), (5, 7), (10, 10)], available((1, 10), [(3, 4), (8, 9)]))


if __name__ == "__main__":
    unittest.main()
