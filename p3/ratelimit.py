"""Abuse protection: fixed-window request limits per client IP and per account, in memory (one worker).

Paid starts (a design, a refinement, another option, a movie, a gallery start) are limited per account and per IP;
sign-in attempts per IP; registration per IP and per email. The limits are generous for a person and tight for a
script; a refused request answers 429 rate_limited with a Retry-After. Behind nginx the client IP is the first
X-Forwarded-For entry (nginx sets it; deploy/production/nginx.conf), else the socket peer. Switched off by
P3_RATE_LIMITS=false (the tests' harness runs without limits unless a test asks for them).
"""

import time

from p3.context import HttpError

Limits = {                      # name: (requests, window in seconds)
    "start:account": (40, 3600),
    "start:ip": (80, 3600),
    "auth:ip": (30, 900),
    "register:ip": (10, 900),
    "register:email": (5, 3600),
    "quote:ip": (30, 900),          # a customer's answer to a quote (the emailed link)
}
MaxWindows = 50_000             # before pruning expired windows


class RateLimited(HttpError):
    def __init__(self, RetryAfterS: int):
        super().__init__(429, "rate_limited", "Too many requests. Please wait a moment and try again.")
        self.RetryAfter = max(1, int(RetryAfterS))


def ClientIp(Request) -> str:
    """The client's address as the reverse proxy saw it (the first X-Forwarded-For entry), else the socket peer."""
    if Request is None:
        return "unknown"
    Forwarded = (Request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if Forwarded:
        return Forwarded
    return Request.client.host if getattr(Request, "client", None) else "unknown"


class RateLimiter:
    def __init__(self, Enabled: bool = True, Limits_: dict | None = None):
        self.Enabled = Enabled
        self.Limits = dict(Limits_ or Limits)
        self._Windows: dict[tuple, list] = {}          # (name, key) -> [window start, count]

    def Hit(self, Name: str, Key: str | None) -> None:
        """Count one request against (Name, Key); raise RateLimited past the limit."""
        if not self.Enabled or not Key or Name not in self.Limits:
            return
        Max, Window = self.Limits[Name]
        Now_ = time.monotonic()
        Slot = self._Windows.get((Name, Key))
        if Slot is None or Now_ - Slot[0] >= Window:
            if len(self._Windows) >= MaxWindows:
                self._Prune(Now_)
            Slot = [Now_, 0]
            self._Windows[(Name, Key)] = Slot
        Slot[1] += 1
        if Slot[1] > Max:
            raise RateLimited(Window - (Now_ - Slot[0]) + 1)

    def _Prune(self, Now_: float) -> None:
        for K, Slot in list(self._Windows.items()):
            if Now_ - Slot[0] >= self.Limits.get(K[0], (0, 0))[1]:
                del self._Windows[K]

    # ── what the routes call ──
    def Start(self, Request, Who) -> None:
        """A paid start (design, refinement, another option, movie, gallery start)."""
        self.Hit("start:ip", ClientIp(Request))
        self.Hit("start:account", getattr(Who, "AccountId", None))

    def Auth(self, Request) -> None:
        """A sign-in attempt."""
        self.Hit("auth:ip", ClientIp(Request))

    def Register(self, Request, Email: str | None) -> None:
        self.Hit("register:ip", ClientIp(Request))
        self.Hit("register:email", (Email or "").strip().lower() or None)
