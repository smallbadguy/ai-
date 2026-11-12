"""限频防刷（内存里的滑动窗口，服务重启后清零）。

保护的是「会触发取数 + AI 的操作」：生成新活动、手动刷新。每次生成一个新活动要调用几次到十几次 AI，所以要防止被反复点
（首页的「刷新全部」2026-11-02 已按用户要求去掉）。默认值（可用环境变量调整，都是「每个登录用户」，没开登录时按全站算一个人）：
  REPLAY_LIMIT_ADD_PER_DAY      每人每天最多生成几个新活动，默认 20
  REPLAY_LIMIT_REFRESH_GAP_MIN  同一个活动两次手动刷新至少隔几分钟，默认 10
每天的定时刷新（service/refresh.py）不受这些限制。
"""
import os
import threading
import time


def _int(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


class RateLimiter:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.adds = {}      # 用户 -> [时间戳]
        self.last = {}      # 活动编号 -> 上次手动刷新时间

    @property
    def add_per_day(self):
        return _int("REPLAY_LIMIT_ADD_PER_DAY", 20)

    @property
    def refresh_gap(self):
        return _int("REPLAY_LIMIT_REFRESH_GAP_MIN", 10) * 60

    def _recent(self, stamps, window):
        now = self.clock()
        stamps[:] = [t for t in stamps if now - t < window]
        return stamps

    def allow_add(self, user, n):
        """生成一个新活动；已经生成过的活动再点「生成」等同于刷新，按刷新的间隔算。"""
        with self.lock:
            stamps = self._recent(self.adds.setdefault(user or "-", []), 86400)
            if self.add_per_day and len(stamps) >= self.add_per_day:
                return False
            if self.refresh_gap and self.clock() - self.last.get(n, -1e12) < self.refresh_gap:
                return False
            stamps.append(self.clock())
            self.last[n] = self.clock()
            return True

    def allow_refresh(self, n):
        with self.lock:
            if self.refresh_gap and self.clock() - self.last.get(n, -1e12) < self.refresh_gap:
                return False
            self.last[n] = self.clock()
            return True
