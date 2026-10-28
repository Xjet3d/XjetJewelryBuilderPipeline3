"""R-16 (owner decision of 2026-10-28): the credits a new account starts with are the Admin's number (Admin → Settings →
Products → Credits, 10 until an Admin sets another). They are counted once, when the account is created — with an email
address or with Google — so a change never touches an account that exists. Registration promises no number; the FAQ
says the number of the moment, and nothing at 0. Charging is unchanged: an account at 0 cannot start paid work."""

from pathlib import Path

import pytest

from p3.oidc import Claims
from tests.conftest import Harness
from tests.test_auth import Browser, FakeIdp, SignedUp, ViaProvider

AdminKey = "starting-credits-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Index = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
async def S(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey, AuthMode="dual")          # Atelier and proto sign in in "dual" mode
    Obj.Ctx.Oidc = {"google": FakeIdp("google")}
    yield Obj
    await Obj.Close()


async def Balance(B) -> dict:
    R = await B.get("/api/token-status")
    assert R.status_code == 200, R.text
    return R.json()


async def SetStarting(H, Value, Headers=Admin):
    return await H.Client.put("/api/admin/credits/starting", json={"starting_credits": Value}, headers=Headers)


async def test_a_new_account_starts_with_the_admins_number_and_an_existing_one_keeps_its_credits(S):
    H = S
    St = (await H.Client.get("/api/admin/credits", headers=Admin)).json()
    assert (St["starting_credits"], St["default_starting_credits"], St["starting_max"]) == (10, 10, 100)
    First, _ = await SignedUp(H, "first@example.com", "First")
    assert (await Balance(First))["max"] == 10                                # until an Admin sets a number: 10
    R = await SetStarting(H, 3)
    assert R.status_code == 200 and R.json()["starting_credits"] == 3, R.text
    Log = (await H.Client.get("/api/admin/products", headers=Admin)).json()["log"]
    assert [(L["value"], bool(L["by"])) for L in Log if L["key"] == "starting_credits"] == [(3, True)]   # logged with who and when
    assert (await Balance(First))["max"] == 10                                # an existing account keeps its credits
    Second, _ = await SignedUp(H, "second@example.com", "Second")
    assert (await Balance(Second))["max"] == 3                                # created by email: the new number
    H.Ctx.Oidc["google"].Next = Claims(Subject="g-new", Email="gina@example.com", EmailVerified=True, Name="Gina")
    G = Browser(H)
    assert await ViaProvider(H, G) == "auth=created"
    assert (await Balance(G))["max"] == 3                                     # created with Google: the same
    assert (await SetStarting(H, 0)).status_code == 200
    Zero, ZeroCsrf = await SignedUp(H, "zero@example.com", "Zero")
    Z = await Balance(Zero)
    assert (Z["max"], Z["remaining"]) == (0, 0)
    Refused = await Zero.post("/api/designs", data={"prompt": "A slim band"}, headers=ZeroCsrf)
    assert Refused.status_code == 402 and Refused.json()["error"]["code"] == "quota_exhausted"   # charging rules unchanged
    assert [(await Balance(B))["max"] for B in (First, Second, G)] == [10, 3, 3]
    for B in (First, Second, G, Zero):
        await B.aclose()


async def test_only_an_admin_sets_a_whole_number_from_0_to_100(S):
    H = S
    for Bad in (-1, 101, 2.5, "5", True, None):
        R = await SetStarting(H, Bad)
        assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_starting_credits", Bad
    assert (await SetStarting(H, 5, Headers={})).status_code == 403
    assert (await H.Client.get("/api/admin/credits", headers=Admin)).json()["starting_credits"] == 10
    assert (await SetStarting(H, 10)).status_code == 200                      # the number it already is: nothing is logged
    assert not [L for L in (await H.Client.get("/api/admin/products", headers=Admin)).json()["log"] if L["key"] == "starting_credits"]


async def test_the_faq_says_the_number_of_the_moment_and_registration_promises_none(S):
    H = S
    Faq = (await H.Client.get("/faq")).text
    assert "A new account starts with 10 credits." in Faq
    await SetStarting(H, 4)
    assert "A new account starts with 4 credits." in (await H.Client.get("/faq")).text
    await SetStarting(H, 1)
    assert "A new account starts with 1 credit." in (await H.Client.get("/faq")).text
    await SetStarting(H, 0)
    Faq = (await H.Client.get("/faq")).text
    assert "A new account starts with" not in Faq and "Choosing a design, its metal and its size is free." in Faq
    # Create account: no number promised, and the page config no longer carries one
    assert "register: 'Your designs are saved to your account.'," in Index and "A new account starts with" not in Index
    assert "starting_credits" not in (await H.Client.get("/")).text
