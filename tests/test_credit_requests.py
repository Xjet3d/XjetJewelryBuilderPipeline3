"""Credit requests: a customer asks for more credits, an Admin approves (the suggested or another number) or rejects.

One pending request per customer; a double click returns it; an approval adds to the existing balance exactly once —
also with a double click, a retry or two Admins at once; a rejection changes nothing; the suggested number is an Admin
setting that never alters past approvals; customers can neither grant credits nor see other customers' requests."""

import threading

import pytest

from p3.accounts import creditrequests as CreditRequests
from p3.context import HttpError
from tests.conftest import Harness

AdminKey = "credit-requests-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}


def _Remaining(H, AccountId=None):
    Q = H.Ctx.Accounts._Quota(AccountId or H.Who.AccountId)
    return Q["remaining"]


def _Spend(H, Units):
    """Use credits the way finished work does (charged), without any generation."""
    H.Ctx.Accounts.Db.Execute("UPDATE accounts SET generations_used = generations_used + ? WHERE account_id = ?",
                              (Units, H.Who.AccountId))


@pytest.fixture
async def H(tmp_path):
    Hh = Harness(tmp_path, AdminKey=AdminKey)
    yield Hh
    await Hh.Close()


async def test_request_approve_adds_to_the_balance_once(H):
    _Spend(H, _Remaining(H))                                           # out of credits
    assert _Remaining(H) == 0
    R = await H.Client.post("/api/credit-requests", json={"source": "blocked_action"})
    assert R.status_code == 200 and R.json()["created"] is True
    Req = R.json()["request"]
    assert Req["status"] == "pending" and R.json()["can_request"] is False
    # a double click / a second tab: the same request, never a second one
    Again = (await H.Client.post("/api/credit-requests", json={})).json()
    assert Again["created"] is False and Again["request"]["id"] == Req["id"]
    assert (await H.Client.get("/api/token-status")).json()["credit_request"]["request"]["status"] == "pending"
    # the Admin sees it with the suggested 5 and the current balance
    L = (await H.Client.get("/api/admin/credit-requests", headers=Admin)).json()
    assert L["pending_count"] == 1 and L["default_credits"] == 5
    assert L["pending"][0]["current_credits"] == 0 and "email" in L["pending"][0]
    Users = (await H.Client.get("/api/admin/users", headers=Admin)).json()["users"]
    assert [U["credit_request_pending"] for U in Users if U["account_id"] == H.Who.AccountId] == [True]
    A = await H.Client.post(f"/api/admin/credit-requests/{Req['id']}/approve", headers=Admin, json={"credits": 5})
    assert A.status_code == 200, A.text
    assert A.json()["request"]["status"] == "approved" and A.json()["request"]["credits_granted"] == 5
    assert A.json()["request"]["resolved_by"] and A.json()["request"]["balance_after"] == 5
    assert _Remaining(H) == 5
    # a second click, a refresh, a retry: refused, nothing granted twice
    B = await H.Client.post(f"/api/admin/credit-requests/{Req['id']}/approve", headers=Admin, json={"credits": 5})
    assert B.status_code == 409 and B.json()["error"]["code"] == "credit_request_resolved"
    assert _Remaining(H) == 5
    Events = H.Ctx.Accounts.AdminActivity(H.Who.AccountId)["events"]
    assert [E["kind"] for E in Events].count("credits_granted") == 1
    assert [E["kind"] for E in Events].count("credit_requested") == 1
    # the customer sees the approval; a new request may follow at once
    S = (await H.Client.get("/api/credit-requests")).json()
    assert S["request"]["status"] == "approved" and S["request"]["credits_granted"] == 5 and S["can_request"] is True
    assert "resolved_by" not in S["request"]


async def test_approval_adds_to_an_existing_balance_and_the_amount_can_change(H):
    _Spend(H, _Remaining(H) - 2)                                       # 2 left
    Req = (await H.Client.post("/api/credit-requests", json={})).json()["request"]
    A = (await H.Client.post(f"/api/admin/credit-requests/{Req['id']}/approve", headers=Admin, json={"credits": 10})).json()
    assert A["request"]["credits_granted"] == 10 and _Remaining(H) == 12


@pytest.mark.parametrize("Bad", [0, -1, 2.5, "5", None, True, 101])
async def test_only_positive_whole_numbers_are_granted(H, Bad):
    Req = (await H.Client.post("/api/credit-requests", json={})).json()["request"]
    Before = _Remaining(H)
    R = await H.Client.post(f"/api/admin/credit-requests/{Req['id']}/approve", headers=Admin, json={"credits": Bad})
    assert R.status_code == 400 and R.json()["error"]["code"] == "invalid_credit_amount"
    assert _Remaining(H) == Before
    assert (await H.Client.get("/api/credit-requests")).json()["request"]["status"] == "pending"


async def test_simultaneous_approvals_grant_once(H):
    _Spend(H, _Remaining(H))
    Req = (await H.Client.post("/api/credit-requests", json={})).json()["request"]
    Results = []

    def Go():
        try:
            CreditRequests.Approve(H.Ctx, Req["id"], 5, "admin-a")
            Results.append("ok")
        except HttpError as E:
            Results.append(E.Code)

    Threads = [threading.Thread(target=Go) for _ in range(8)]
    for T in Threads:
        T.start()
    for T in Threads:
        T.join()
    assert sorted(Results) == ["credit_request_resolved"] * 7 + ["ok"]
    assert _Remaining(H) == 5


async def test_reject_keeps_the_balance_and_waits_before_a_new_request(H):
    _Spend(H, _Remaining(H))
    Req = (await H.Client.post("/api/credit-requests", json={})).json()["request"]
    R = (await H.Client.post(f"/api/admin/credit-requests/{Req['id']}/reject", headers=Admin, json={"reason": "test"})).json()
    assert R["request"]["status"] == "rejected" and R["request"]["reason"] == "test" and _Remaining(H) == 0
    assert (await H.Client.post(f"/api/admin/credit-requests/{Req['id']}/approve", headers=Admin,
                                json={"credits": 5})).status_code == 409
    S = (await H.Client.get("/api/credit-requests")).json()
    assert S["request"]["status"] == "rejected" and S["can_request"] is False and S["next_allowed_at"]
    assert "reason" not in S["request"]                                # the Admin's note stays in the Admin
    Again = await H.Client.post("/api/credit-requests", json={})
    assert Again.status_code == 429 and Again.json()["error"]["code"] == "credit_request_limit"
    # after the waiting time a new request may be sent
    H.Ctx.Accounts.Db.Execute("UPDATE credit_requests SET requested_at = '2020-01-01T00:00:00.000+00:00', "
                              "resolved_at = '2020-01-01T00:00:00.000+00:00'")
    assert (await H.Client.post("/api/credit-requests", json={})).json()["created"] is True


async def test_at_most_three_requests_a_day(H):
    for I in range(3):
        Req = (await H.Client.post("/api/credit-requests", json={})).json()["request"]
        assert (await H.Client.post(f"/api/admin/credit-requests/{Req['id']}/approve", headers=Admin,
                                    json={"credits": 1})).status_code == 200
    R = await H.Client.post("/api/credit-requests", json={})
    assert R.status_code == 429 and R.json()["error"]["code"] == "credit_request_limit"


async def test_the_default_is_an_admin_setting_and_never_changes_past_approvals(H):
    Req = (await H.Client.post("/api/credit-requests", json={})).json()["request"]
    await H.Client.post(f"/api/admin/credit-requests/{Req['id']}/approve", headers=Admin, json={"credits": 5})
    D = await H.Client.put("/api/admin/credit-requests/default", headers=Admin, json={"default_credits": 10})
    assert D.status_code == 200 and D.json()["default_credits"] == 10
    assert D.json()["history"][0]["credits_granted"] == 5
    for Bad in (0, 2.5, "7", 101):
        R = await H.Client.put("/api/admin/credit-requests/default", headers=Admin, json={"default_credits": Bad})
        assert R.status_code == 400
    assert (await H.Client.get("/api/admin/credit-requests", headers=Admin)).json()["default_credits"] == 10


async def test_customers_cannot_grant_or_see_others(H):
    Req = (await H.Client.post("/api/credit-requests", json={"credits": 99, "status": "approved"})).json()["request"]
    Before = _Remaining(H)
    assert Req["status"] == "pending"
    # no Admin key: refused
    for Path in (f"/api/admin/credit-requests/{Req['id']}/approve", f"/api/admin/credit-requests/{Req['id']}/reject"):
        assert (await H.Client.post(Path, json={"credits": 5})).status_code in (401, 403)
    assert (await H.Client.get("/api/admin/credit-requests")).status_code in (401, 403)
    assert _Remaining(H) == Before
    # another customer sees only their own (nothing)
    Tok2, _Who2 = H.Ctx.Accounts.IssueToken("other", MaxGenerations=10, Email="other@example.com")
    Other = (await H.Client.get("/api/credit-requests", headers={"X-Access-Token": Tok2})).json()
    assert Other["request"] is None and Other["can_request"] is True


async def test_out_of_credits_still_refuses_paid_work_and_nothing_is_granted_by_asking(H):
    _Spend(H, _Remaining(H))
    await H.Client.post("/api/credit-requests", json={"source": "blocked_action"})
    R = await H.Client.post("/api/designs", data={"prompt": "A slim twisted band"})
    assert R.status_code == 402 and R.json()["error"]["code"] == "quota_exhausted"
    assert _Remaining(H) == 0
