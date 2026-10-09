"""Abuse protection: fixed-window request limits per client IP and per account, in memory (one worker).

Paid starts (a design, a refinement, another option, a movie, a gallery start) are limited per account and per IP;
sign-in attempts per IP; registration per IP and per email. The limits are generous for a person and tight for a
script; a refused request answers 429 rate_limited with a Retry-After. The client IP is the first address in the
proxy chain that is not a proxy we trust (ClientIp): X-Forwarded-For entries a client wrote itself are never believed.
Switched off by P3_RATE_LIMITS=false (the tests' harness runs without limits unless a test asks for them).
"""

import ipaddress
import logging
import os
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


Logger = logging.getLogger("p3.ratelimit")
# Cloudflare's published edge ranges (www.cloudflare.com/ips-v4 and /ips-v6, checked 2026-10-09). A request that reaches
# the site through Cloudflare comes from one of them, and Cloudflare adds the visitor's address to X-Forwarded-For.
CloudflareRanges = ("173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
                    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
                    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
                    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
                    "2a06:98c0::/29", "2c0f:f248::/32")
_Trusted: tuple | None = None


def _Networks(Text: str, What: str) -> tuple:
    Out = []
    for Part in (Text or "").replace(";", ",").split(","):
        if Part.strip():
            try:
                Out.append(ipaddress.ip_network(Part.strip(), strict=False))
            except ValueError:
                Logger.warning("%s: not an address or range: %s", What, Part.strip())
    return tuple(Out)


def TrustedNetworks() -> tuple:
    """The proxies whose X-Forwarded-For entries are believed: this machine (nginx in front of uvicorn), the addresses or
    ranges in P3_TRUSTED_PROXIES (e.g. an nginx host on the LAN) and Cloudflare's edge."""
    global _Trusted
    if _Trusted is None:
        _Trusted = ((ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))
                    + _Networks(os.environ.get("P3_TRUSTED_PROXIES", ""), "P3_TRUSTED_PROXIES")
                    + _Networks(",".join(CloudflareRanges), "CloudflareRanges"))
    return _Trusted


def TrustedDescription() -> list[str]:
    Own = [str(N) for N in _Networks(os.environ.get("P3_TRUSTED_PROXIES", ""), "P3_TRUSTED_PROXIES")]
    return ["127.0.0.0/8", "::1/128", *Own, f"Cloudflare ({len(CloudflareRanges)} ranges)"]


def _IsTrusted(Ip: str) -> bool:
    try:
        A = ipaddress.ip_address(Ip.strip().strip("[]"))
    except ValueError:
        return False
    if A.version == 6 and A.ipv4_mapped:
        A = A.ipv4_mapped
    return any(A in N for N in TrustedNetworks() if N.version == A.version)


def ClientIp(Request) -> str:
    """The visitor's address: walk the chain from this server outward (the socket peer, then X-Forwarded-For from the
    right) and take the first hop that is not a proxy we trust. Entries left of it are whatever the client wrote and are
    never believed (the first entry used to be taken as is, so anyone could choose their own rate-limit key)."""
    if Request is None:
        return "unknown"
    Peer = Request.client.host if getattr(Request, "client", None) else ""
    Hops = [X.strip() for X in (Request.headers.get("x-forwarded-for") or "").split(",") if X.strip()] + ([Peer] if Peer else [])
    for Ip in reversed(Hops):
        if not _IsTrusted(Ip):
            return Ip
    return Hops[0] if Hops else "unknown"


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
