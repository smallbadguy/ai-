"""限频（service/ratelimit.py）的测试。"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from service.ratelimit import RateLimiter  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Limits(unittest.TestCase):
    def setUp(self):
        self.c = Clock()
        self.r = RateLimiter(clock=self.c)

    def test_same_campaign_manual_refresh_needs_a_gap(self):
        self.assertTrue(self.r.allow_refresh("5023"))
        self.assertFalse(self.r.allow_refresh("5023"))
        self.assertTrue(self.r.allow_refresh("5024"))            # 别的活动不受影响
        self.c.t += 601
        self.assertTrue(self.r.allow_refresh("5023"))

    def test_new_campaigns_per_user_per_day(self):
        for i in range(20):
            self.assertTrue(self.r.allow_add("a@x.com", str(i)))
        self.assertFalse(self.r.allow_add("a@x.com", "99"))
        self.assertTrue(self.r.allow_add("b@x.com", "99"))       # 另一个人照常
        self.c.t += 86401
        self.assertTrue(self.r.allow_add("a@x.com", "100"))      # 一天后恢复

    def test_env_overrides_and_zero_disables(self):
        import os
        os.environ["REPLAY_LIMIT_REFRESH_GAP_MIN"] = "0"
        os.environ["REPLAY_LIMIT_ADD_PER_DAY"] = "1"
        try:
            self.assertTrue(self.r.allow_refresh("1"))
            self.assertTrue(self.r.allow_refresh("1"))
            self.assertTrue(self.r.allow_add("a@x.com", "2"))
            self.assertFalse(self.r.allow_add("a@x.com", "3"))
        finally:
            del os.environ["REPLAY_LIMIT_REFRESH_GAP_MIN"], os.environ["REPLAY_LIMIT_ADD_PER_DAY"]


if __name__ == "__main__":
    unittest.main()
