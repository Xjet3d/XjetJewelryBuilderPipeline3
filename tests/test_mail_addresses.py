"""Where customer mail goes (p3/settings.py DefaultSupportEmail, p3/mail.py ContactAddress): the shared inbox
atelier@xjet3d.com unless the environment names another address; P3_STAFF_NOTIFY_EMAILS=none switches the team
notifications off (proto)."""

import json
import re

import pytest

from p3.settings import LoadSettings, StaffRecipients
from tests.conftest import Harness

AdminKey = "mail-admin-key"
Admin = {"Authorization": f"Bearer {AdminKey}"}
Vars = ("P3_SUPPORT_EMAIL", "MAIL_REPLY_TO", "P3_STAFF_NOTIFY_EMAILS")


@pytest.fixture
def Clean(monkeypatch):
    for V in Vars:
        monkeypatch.delenv(V, raising=False)
    return monkeypatch


def test_nothing_configured_means_the_shared_inbox_everywhere(Clean, tmp_path):
    S = LoadSettings(DataDir=tmp_path)
    assert (S.SupportEmail, S.ReplyTo, S.StaffNotifyEmails) == ("atelier@xjet3d.com", "atelier@xjet3d.com", ("atelier@xjet3d.com",))


def test_the_environment_overrides_and_none_switches_the_team_notifications_off(Clean, tmp_path):
    Clean.setenv("P3_SUPPORT_EMAIL", "help@example.com")
    S = LoadSettings(DataDir=tmp_path)
    assert (S.SupportEmail, S.ReplyTo, S.StaffNotifyEmails) == ("help@example.com", "help@example.com", ("help@example.com",))
    Clean.setenv("MAIL_REPLY_TO", "replies@example.com")
    Clean.setenv("P3_STAFF_NOTIFY_EMAILS", "orders@example.com, sales@example.com")
    S = LoadSettings(DataDir=tmp_path)
    assert S.ReplyTo == "replies@example.com" and S.StaffNotifyEmails == ("orders@example.com", "sales@example.com")
    for Off in ("none", "NONE", " off "):
        Clean.setenv("P3_STAFF_NOTIFY_EMAILS", Off)
        assert LoadSettings(DataDir=tmp_path).StaffNotifyEmails == ()
    assert StaffRecipients("", "atelier@xjet3d.com") == ("atelier@xjet3d.com",) and StaffRecipients("none", "a@b.co") == ()


async def test_the_site_and_the_admin_show_where_customer_mail_goes(Clean, tmp_path):
    H = Harness(tmp_path, AdminKey=AdminKey, PublicBaseUrl="https://atelier.example.com")
    try:
        Page = (await H.Client.get("/")).text
        assert '"support_email": "atelier@xjet3d.com"' in Page                     # Contact Us
        Ld = [json.loads(M) for M in re.findall(r'<script type="application/ld\+json">(.*?)</script>', Page, re.S)]
        assert Ld[0]["contactPoint"]["email"] == "atelier@xjet3d.com"               # search engines: the support contact
        R = (await H.Client.get("/api/admin/mail-addresses", headers=Admin)).json()
        assert (R["support_email"], R["support_source"], R["reply_to"], R["reply_to_source"]) == (
            "atelier@xjet3d.com", "default", "atelier@xjet3d.com", "default")
        assert R["team"] == [] and R["sender"] == "no-reply@xjet3d.com"            # the test harness names no team
        assert (await H.Client.get("/api/admin/mail-addresses")).status_code in (401, 403)
    finally:
        await H.Close()
