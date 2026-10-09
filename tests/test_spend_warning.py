"""The daily AI spend warning (p3/spendwarning.py) and the client address behind trusted proxies (p3/ratelimit.py)."""

import pytest

from p3 import ratelimit as RateLimit
from p3 import spendwarning as SpendWarning
from tests.conftest import Harness

AdminKey = "spend-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


@pytest.fixture
async def HW(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl="https://atelier.example.com")
    yield Obj
    await Obj.Close()


def _Spend(H, Usd):
    H.Ctx.Accounts.RecordUsage(H.Who.AccountId, "generation", 1, "ref", Provider="fal", Endpoint="fal-ai/nano-banana-pro",
                               CostUsd=Usd, CostSource="test", Internal=False)


async def test_the_warning_is_set_in_the_admin_and_sent_once_a_day(HW, monkeypatch):
    H = HW
    St = (await H.Client.get("/api/admin/spend-warning", headers=Admin)).json()
    assert St["threshold_usd"] is None and St["to"] == "" and St["live"] is False          # off by default; mock here
    for Body, Code in (({"threshold_usd": 0.5, "to": "ops@example.com"}, "invalid_threshold"), ({"threshold_usd": "x", "to": "a@b.co"}, "invalid_threshold"),
                       ({"threshold_usd": 50, "to": "not-an-email"}, "invalid_email"), ({"threshold_usd": 50, "to": ""}, "email_required")):
        R = await H.Client.put("/api/admin/spend-warning", json=Body, headers=Admin)
        assert R.status_code == 400 and R.json()["error"]["code"] == Code, Body
    St = (await H.Client.put("/api/admin/spend-warning", json={"threshold_usd": 50, "to": "ops@example.com"}, headers=Admin)).json()
    assert (St["threshold_usd"], St["to"]) == (50.0, "ops@example.com")
    assert H.Ctx.Db.One("SELECT COUNT(*) AS n FROM product_settings_log WHERE key = 'ai_spend_warning'")["n"] == 1   # logged
    # Mock mode spends nothing: no warning, whatever is recorded
    _Spend(H, 80)
    assert SpendWarning.Check(H.Ctx, Background=False) is None
    # Live: below the threshold nothing; past it one email, once a day; the request is never stopped
    monkeypatch.setattr(H.Ctx.Provider, "Name", "fal", raising=False)
    H.Ctx.Accounts.Db.Execute("DELETE FROM usage_events")
    _Spend(H, 30)
    assert SpendWarning.Check(H.Ctx, Background=False) is None
    _Spend(H, 25)
    Sent = SpendWarning.Check(H.Ctx, Background=False)
    assert Sent and Sent["spend_usd"] == 55.0 and Sent["to"] == "ops@example.com"
    [Mail] = [M for M in H.App.state.Mailer.List(50) if M["to"] == "ops@example.com"]
    assert Mail["subject"] == "AI spend today passed $50.00 — atelier.example.com" and "$55.00" in Mail["html"]
    assert "does not stop or slow anything" in Mail["html"] and "https://atelier.example.com/admin#/dashboard" in Mail["html"]
    _Spend(H, 40)
    assert SpendWarning.Check(H.Ctx, Background=False) is None                              # once a day
    St = (await H.Client.get("/api/admin/spend-warning", headers=Admin)).json()
    assert St["last"]["spend_usd"] == 55.0 and St["last"]["delivery"].startswith("sent") and St["spend_today_usd"] == pytest.approx(95.0)
    # A mailer that fails is recorded; nothing raises
    monkeypatch.setattr(H.Ctx.Products, "Get", lambda K, _Real=H.Ctx.Products.Get: {} if K == SpendWarning.SentKey else _Real(K))
    def Boom(*_A, **_K):
        raise OSError("relay down")
    monkeypatch.setattr(H.Ctx.Mailer, "Send", Boom)
    Sent = SpendWarning.Check(H.Ctx, Background=False)
    assert Sent and "failed" in H.Ctx.Db.One("SELECT value_json FROM product_settings WHERE key = 'ai_spend_warning_sent'")["value_json"]
    # Off again
    St = (await H.Client.put("/api/admin/spend-warning", json={"threshold_usd": None, "to": ""}, headers=Admin)).json()
    assert St["threshold_usd"] is None


def test_every_recorded_submission_checks_the_warning(monkeypatch, tmp_path):
    from p3 import credits as Credits
    Seen = []
    monkeypatch.setattr(SpendWarning, "Check", lambda Ctx, Background=True: Seen.append(Ctx))
    class A:
        def RecordUsage(self, *_A, **_K):
            pass
    class Ctx:
        Accounts, Provider, AiPrices = A(), type("P", (), {"Name": "fal"})(), None
    Credits.RecordUsage(Ctx, "acct", "generation", "ref", "fal-ai/nano-banana-pro")
    assert Seen == [Ctx]


class _Req:
    def __init__(self, Peer, Xff=None):
        self.client = type("C", (), {"host": Peer})()
        self.headers = {"x-forwarded-for": Xff} if Xff else {}


def test_the_client_address_is_the_first_hop_that_is_not_a_trusted_proxy(monkeypatch):
    # Behind Cloudflare and nginx: a forged entry on the left is ignored, the visitor is the first untrusted hop
    assert RateLimit.ClientIp(_Req("127.0.0.1", "6.6.6.6, 198.51.100.7, 172.70.1.1")) == "198.51.100.7"
    assert RateLimit.ClientIp(_Req("127.0.0.1", "6.6.6.6, 2001:db8::1, 2606:4700::6810:1")) == "2001:db8::1"
    # nginx alone (proto): the address nginx appended
    assert RateLimit.ClientIp(_Req("127.0.0.1", "6.6.6.6, 203.0.113.9")) == "203.0.113.9"
    # A direct visitor (no trusted proxy in between): its own address, whatever header it sends
    assert RateLimit.ClientIp(_Req("203.0.113.5", "1.2.3.4")) == "203.0.113.5"
    assert RateLimit.ClientIp(_Req("127.0.0.1")) == "127.0.0.1" and RateLimit.ClientIp(None) == "unknown"
    assert RateLimit.ClientIp(_Req("127.0.0.1", "garbage, 203.0.113.9")) == "203.0.113.9"
    # An nginx host on the LAN is trusted only when configured
    monkeypatch.setattr(RateLimit, "_Trusted", None)
    monkeypatch.setenv("P3_TRUSTED_PROXIES", "10.0.0.5, 192.168.10.0/24")
    assert RateLimit.ClientIp(_Req("10.0.0.5", "203.0.113.9")) == "203.0.113.9"
    assert RateLimit.ClientIp(_Req("10.0.0.6", "203.0.113.9")) == "10.0.0.6"
    assert "192.168.10.0/24" in RateLimit.TrustedDescription()
    monkeypatch.setattr(RateLimit, "_Trusted", None)


async def test_the_admin_sees_its_own_address(HW):
    H = HW
    R = (await H.Client.get("/api/admin/client-ip", headers={**Admin, "X-Forwarded-For": "6.6.6.6, 203.0.113.9", "CF-Ray": "abc"})).json()
    assert R["address"] == "203.0.113.9" and R["via_cloudflare"] is True and R["x_forwarded_for"] == "6.6.6.6, 203.0.113.9"
    assert (await H.Client.get("/api/admin/client-ip")).status_code in (401, 403)
