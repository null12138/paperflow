import unittest

from paperflow.net import RateLimiter


class RateLimiterTests(unittest.TestCase):
    def test_requested_rate_is_converted_to_interval(self):
        self.assertEqual(RateLimiter(60).interval, 1.0)
        self.assertEqual(RateLimiter(30).interval, 2.0)

    def test_rate_is_bounded(self):
        self.assertEqual(RateLimiter(0).interval, 60.0)
        self.assertEqual(RateLimiter(9999).interval, 1 / 60)


if __name__ == "__main__":
    unittest.main()
