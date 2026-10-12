"""Behind proto's front proxy (HTTPS to tron since tron's HTTPS policy of 2026-10-11) the visitor's own scheme arrives
as X-Forwarded-Scheme, because tron's nginx rewrites X-Forwarded-Proto to its own scheme. That scheme decides the
admin cookie's Secure flag and HSTS (p3/adminauth.py BrowserScheme); without the header nothing changes."""

import re

from p3 import adminauth as AdminAuth
from tests.conftest import Harness

AdminKey = "proxy-scheme-admin-key"


def _SessionCookie(R) -> str:
    return next(C for C in R.headers.get_list("set-cookie") if C.startswith(AdminAuth.CookieName + "="))


def _Secure(C: str) -> bool:
    return bool(re.search(r";\s*secure\s*(;|$)", C, re.I))


async def test_the_visitors_scheme_decides_the_secure_cookie_and_hsts(tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey)
    try:
        async def Login(Headers):
            R = await H.Client.post("/api/admin/login", json={"key": AdminKey}, headers=Headers)
            assert R.status_code == 200, R.text
            return _SessionCookie(R)

        async def Hsts(Headers):
            return "strict-transport-security" in (await H.Client.get("/", headers=Headers)).headers

        # proto over plain http: tron says https, proto says http -> no Secure flag (the browser would drop it), no HSTS
        Plain = {"X-Forwarded-Proto": "https", "X-Forwarded-Scheme": "http"}
        assert not _Secure(await Login(Plain)) and not await Hsts(Plain)
        # proto over https
        Tls = {"X-Forwarded-Proto": "https", "X-Forwarded-Scheme": "https"}
        assert _Secure(await Login(Tls)) and await Hsts(Tls)
        # Atelier / tron directly (no X-Forwarded-Scheme): X-Forwarded-Proto as before
        assert _Secure(await Login({"X-Forwarded-Proto": "https"})) and await Hsts({"X-Forwarded-Proto": "https"})
        assert not _Secure(await Login({})) and not await Hsts({})
        # anything else in the header is ignored
        assert _Secure(await Login({"X-Forwarded-Proto": "https", "X-Forwarded-Scheme": "gopher"}))
    finally:
        await H.Close()
