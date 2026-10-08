"""Customer accounts after a removal (2026-10-08). A removed address gets no dead sign-in code: the page says to contact
XJet. Admin "Create a customer" never makes a second account for a removed address: it offers to restore that account.
Restoring brings back the same account with its history, credits and activity; activating turns its code on again.
E-mail sign-in works for every active account with the address, also a customer created in the Admin."""

import re

import pytest

from p3.registration import InactiveMessage, RegisterMessage, RemovedMessage
from tests.conftest import Harness

AdminKey = "accounts-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Anon = {"X-Access-Token": ""}
Email = "yaka@example.com"


@pytest.fixture
async def HA(tmp_path):
    Obj = Harness(tmp_path, AdminKey=AdminKey)
    yield Obj
    await Obj.Close()


def _Mail(H) -> list[dict]:
    return H.App.state.Mailer.List()                     # newest first


def _Accounts(H, Address: str) -> list[dict]:
    return H.Ctx.Accounts.Db.All("SELECT account_id, source, removed_at FROM accounts WHERE lower(email) = ?",
                                 (Address.lower(),))


def _Events(H, AccountId: str) -> list[str]:
    return [E["kind"] for E in H.Ctx.Accounts.AdminActivity(AccountId)["events"]]


async def _Registered(H, Name="Yakir Tubul", Address=Email) -> tuple[str, str, str]:
    """A self-registered, verified customer: (account id, sign-in code, verification secret)."""
    R = await H.Client.post("/api/register", json={"Name": Name, "Email": Address}, headers=Anon)
    assert R.status_code == 200, R.text
    Secret = re.search(r'/verify\?token=([A-Za-z0-9_\-]+)"', _Mail(H)[0]["html"]).group(1)
    Page = (await H.Client.get(f"/verify?token={Secret}", headers=Anon)).text
    Code = re.search(r'<div class="token">([A-Z]{6})</div>', Page).group(1)
    return _Accounts(H, Address)[0]["account_id"], Code, Secret


async def _SignIn(H, Code: str):
    return await H.Client.post("/api/register-token", json={"Token": Code, "Name": "", "Email": ""}, headers=Anon)


async def _Act(H, AccountId: str, Action: str):
    R = await H.Client.post(f"/api/admin/users/{AccountId}/{Action}", headers=Admin)
    assert R.status_code == 200, R.text
    return R.json()


async def test_removed_then_registering_again_sends_no_dead_code_and_says_to_contact_xjet(HA):
    H = HA
    Acc, Code, Secret = await _Registered(H)
    assert (await _SignIn(H, Code)).json()["ok"]
    await _Act(H, Acc, "remove")
    Sent = len(_Mail(H))
    R = await H.Client.post("/api/register", json={"Name": "Yakir Tubul", "Email": "YAKA@example.com"}, headers=Anon)
    assert R.status_code == 403 and R.json()["error"] == {
        "code": "account_removed", "message": "This account was removed. Please contact XJet to restore access."}
    assert len(_Mail(H)) == Sent                                       # nothing e-mailed: no dead code
    assert [A["account_id"] for A in _Accounts(H, Email)] == [Acc]     # and no second account
    assert (await _SignIn(H, Code)).status_code == 403                 # the old code stays off
    assert "register_blocked" in _Events(H, Acc)                       # the Admin sees the attempt on the account
    Page = (await H.Client.get(f"/verify?token={Secret}", headers=Anon)).text   # an old e-mail's link: nothing comes back on
    assert "Account removed" in Page and "contact XJet to restore access" in Page and 'class="token"' not in Page
    assert RemovedMessage("help@xjet3d.com") == "This account was removed. Please contact XJet at help@xjet3d.com to restore access."


async def test_admin_create_with_a_removed_address_offers_to_restore_it_and_makes_no_second_account(HA):
    H = HA
    Acc, Code, _ = await _Registered(H)
    await _Act(H, Acc, "remove")
    R = await H.Client.post("/api/admin/users", json={"Name": "Yakir T", "Email": "Yaka@Example.com", "MaxGenerations": 50},
                            headers=Admin)
    assert R.status_code == 409
    E = R.json()["error"]
    assert E["code"] == "removed_account" and "Restore that account instead" in E["message"]
    assert E["account"]["account_id"] == Acc and E["account"]["name"] == "Yakir Tubul" and E["account"]["removed_at"]
    assert [A["account_id"] for A in _Accounts(H, Email)] == [Acc]
    # an address with a live account is still a duplicate, as before
    assert (await H.Client.post("/api/admin/users", json={"Name": "Lee", "Email": "lee@example.com"}, headers=Admin)).status_code == 200
    R = await H.Client.post("/api/admin/users", json={"Name": "Lee 2", "Email": "LEE@example.com"}, headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] == "duplicate_email"
    Page = await H.Client.get("/admin")                                # the Admin page offers the restore
    assert "Restore existing account" in Page.text and "restoreExisting()" in Page.text


async def test_with_two_removed_accounts_the_one_the_customer_used_is_offered_not_an_empty_duplicate(HA):
    """atelier, 2026-10-08: the customer's account was removed, a duplicate was made for the address and removed too.
    The account offered for restore — and the one a sign-in attempt is recorded on — is the one the customer used."""
    H = HA
    Acc, Code, _ = await _Registered(H)
    assert (await _SignIn(H, Code)).json()["ok"]                         # the customer really used it
    await _Act(H, Acc, "remove")
    Twin = (await H.Client.post("/api/admin/users", json={"Name": "Twin", "Email": "twin@example.com"}, headers=Admin)).json()
    R = await H.Client.patch(f"/api/admin/users/{Twin['account_id']}", json={"Name": "Twin", "Email": Email, "MaxGenerations": 10},
                             headers=Admin)
    assert R.status_code == 200
    await _Act(H, Twin["account_id"], "remove")                          # removed after the original
    R = await H.Client.post("/api/admin/users", json={"Name": "Again", "Email": Email}, headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["account"]["account_id"] == Acc
    R = await H.Client.post("/api/register", json={"Name": "", "Email": Email}, headers=Anon)
    assert R.status_code == 403 and R.json()["error"]["code"] == "account_removed"
    assert "register_blocked" in _Events(H, Acc) and "register_blocked" not in _Events(H, Twin["account_id"])


async def test_restore_brings_back_the_same_account_with_its_history_and_activate_turns_its_code_on(HA):
    H = HA
    Acc, Code, _ = await _Registered(H)
    Tok = {"X-Access-Token": Code}
    R = await H.Client.post("/api/designs", data={"prompt": "A slim twisted band with a small leaf motif"}, headers=Tok)
    assert R.status_code == 200, R.text
    await H.Idle()
    Before = H.Ctx.Accounts.AdminGet(Acc)
    Usage = H.Ctx.Accounts.AdminActivity(Acc)["usage"]
    Designs = H.Ctx.Db.All("SELECT id FROM designs WHERE owner_account_id = ?", (Acc,))
    assert Before["used"] >= 1 and len(Designs) == 1 and Usage

    await _Act(H, Acc, "remove")
    # Restore: the same account, still off until activated (no dead code by e-mail meanwhile)
    Restored = await _Act(H, Acc, "restore")
    assert Restored["account_id"] == Acc and Restored["status"] == "inactive"
    assert (await _SignIn(H, Code)).status_code == 403
    R = await H.Client.post("/api/register", json={"Name": "", "Email": Email}, headers=Anon)
    assert R.status_code == 403 and R.json()["error"] == {"code": "account_inactive", "message": InactiveMessage()}

    # Activate: its own code works again, and e-mail sign-in sends that same code
    await _Act(H, Acc, "activate")
    assert (await _SignIn(H, Code)).json()["ok"]
    After = H.Ctx.Accounts.AdminGet(Acc)
    assert (After["used"], After["max"], After["created_at"], After["name"], After["email"]) == \
        (Before["used"], Before["max"], Before["created_at"], Before["name"], Before["email"])
    assert H.Ctx.Accounts.AdminActivity(Acc)["usage"] == Usage
    assert H.Ctx.Db.All("SELECT id FROM designs WHERE owner_account_id = ?", (Acc,)) == Designs
    Kinds = _Events(H, Acc)
    assert Kinds.index("removed") < Kinds.index("restored") < Kinds.index("activated")
    R = await H.Client.post("/api/register", json={"Name": "", "Email": Email}, headers=Anon)
    assert R.status_code == 200 and R.json() == {"status": "verification_sent", "message": RegisterMessage(Email)}
    assert _Mail(H)[0]["subject"] == "Your XJet Atelier sign-in code" and Code in _Mail(H)[0]["html"]

    # Restore is refused while another live account has the address (the Admin removes that one first)
    await _Act(H, Acc, "remove")
    Other = (await H.Client.post("/api/admin/users", json={"Name": "Twin", "Email": "twin@example.com"}, headers=Admin)).json()
    R = await H.Client.patch(f"/api/admin/users/{Other['account_id']}", json={"Name": "Twin", "Email": Email, "MaxGenerations": 10},
                             headers=Admin)
    assert R.status_code == 200
    R = await H.Client.post(f"/api/admin/users/{Acc}/restore", headers=Admin)
    assert R.status_code == 409 and R.json()["error"]["code"] == "duplicate_email"


async def test_email_sign_in_sends_an_admin_created_customer_their_code_and_a_switched_off_one_nothing(HA):
    H = HA
    R = await H.Client.post("/api/admin/users", json={"Name": "Dana Admin", "Email": "dana@example.com", "MaxGenerations": 25},
                            headers=Admin)
    assert R.status_code == 200, R.text
    Acc, Code = R.json()["account_id"], R.json()["token"]
    Sent = len(_Mail(H))
    R = await H.Client.post("/api/register", json={"Name": "Dana", "Email": "Dana@Example.com"}, headers=Anon)
    assert R.status_code == 200 and R.json() == {"status": "verification_sent", "message": RegisterMessage("Dana@Example.com")}
    Mail = _Mail(H)
    assert len(Mail) == Sent + 1 and Mail[0]["to"] == "Dana@Example.com"
    assert Mail[0]["subject"] == "Your XJet Atelier sign-in code" and Code in Mail[0]["html"]   # the Admin-made code
    assert [A["account_id"] for A in _Accounts(H, "dana@example.com")] == [Acc]   # no self-registration beside it
    S = await _SignIn(H, Code)
    assert S.json()["ok"] and S.json()["max"] == 25
    assert "code_emailed" in _Events(H, Acc)
    # Switched off by XJet: no dead code by e-mail either, and no new account
    await _Act(H, Acc, "deactivate")
    R = await H.Client.post("/api/register", json={"Name": "Dana", "Email": "dana@example.com"}, headers=Anon)
    assert R.status_code == 403 and R.json()["error"] == {"code": "account_inactive", "message": InactiveMessage()}
    assert len(_Mail(H)) == Sent + 1 and len(_Accounts(H, "dana@example.com")) == 1
    # A new address still registers as before (verification link)
    R = await H.Client.post("/api/register", json={"Name": "New", "Email": "new@example.com"}, headers=Anon)
    assert R.status_code == 200 and "/verify?token=" in _Mail(H)[0]["html"]
