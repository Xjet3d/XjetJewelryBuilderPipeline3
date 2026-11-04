"""Checkout's phone field and shipping country (4 Nov 2026): a dialling code with the country's flag beside the number,
and the country picked from a list with flags — one source of truth for the codes (p3/addressing.py DialCodes), a flag
per shipping country (web/vendor/flags, flag-icons 7.2.3, MIT), and the same order format as before."""

import re
from xml.dom import minidom

import pytest

from p3 import addressing as Addressing
from p3.orders import OrderService as Orders
from p3.settings import RepoRoot
from tests.conftest import Harness

Web = RepoRoot / "web"
Flags = Web / "vendor" / "flags"
Index = (Web / "index.html").read_text(encoding="utf-8")
App = (Web / "app.js").read_text(encoding="utf-8")


@pytest.fixture
async def H(tmp_path):
    Obj = Harness(tmp_path)
    yield Obj
    await Obj.Close()


async def test_checkout_sends_each_country_with_its_dialling_code(H):
    Await = await H.NewDesign("Twisted rope band", product="ring")
    Cus = (await H.Client.post(f"/api/designs/{Await['design_id']}/customize", json={"candidate_id": Await["candidates"][0]["id"]})).json()
    await H.Ctx.Runner.WaitIdle(10)
    await H.Client.patch(f"/api/customizations/{Cus['id']}", json={"ring_size": 7, "material_id": "silver", "quantity": 1})
    await H.Client.post("/api/bag", json={"customization_id": Cus["id"]})
    Info = (await H.Client.get("/api/checkout")).json()
    Codes = [C["code"] for C in Info["countries"]]
    assert Codes == [C for C, _ in Addressing.Countries]                       # the same list, in the same order
    By = {C["code"]: C for C in Info["countries"]}
    assert all(re.fullmatch(r"\+\d{1,3}", C["dial"]) for C in Info["countries"])
    assert (By["IL"]["dial"], By["US"]["dial"], By["GB"]["dial"], By["CA"]["dial"]) == ("+972", "+1", "+44", "+1")
    assert By["IT"].get("keep_zero") is True and "keep_zero" not in By["IL"]   # +39 06 … keeps its 0; +972 54 … drops it
    assert set(Addressing.DialCodes) == set(Addressing.CountryNames)          # no country without a code, no code without one


def test_a_phone_made_of_a_code_and_a_number_is_one_the_order_accepts():
    """The page sends "<code> <number without its national 0>" (app.js _composePhone): every code passes the server's rule."""
    for Code in sorted(set(Addressing.DialCodes.values())):
        for Number in ("54 123 4567", "54-123-4567", "(20) 7946 0958"):
            _C, Problems = Orders.ValidateCustomer({"first_name": "Dana", "last_name": "Levi", "email": "dana@example.com",
                                                   "phone": f"{Code} {Number}"})
            assert not Problems, (Code, Number, Problems)
    _C, Problems = Orders.ValidateCustomer({"first_name": "D", "last_name": "L", "email": "d@example.com", "phone": "+972"})
    assert [P["field"] for P in Problems] == ["phone"] and "(with the country code)" not in Problems[0]["message"]
    # the page drops the national 0 except in Italy, keeps a number typed in full, and moves a typed code to the picker
    Compose = App[App.index("_composePhone() {"):App.index("coPhoneInput() {")]
    assert "if (!c.keep_zero) n = n.replace(/^0\\s*(?=\\d)/, '');" in Compose and "n.startsWith('+')" in Compose
    assert "this._composePhone();                                           // the code chosen and the number typed" in App


def test_every_shipping_country_has_a_plain_flag():
    Assert = []
    for Code, _ in Addressing.Countries:
        P = Flags / f"{Code.lower()}.svg"
        assert P.exists(), Code
        T = P.read_text(encoding="utf-8")
        assert minidom.parseString(T.encode("utf-8")).documentElement.tagName == "svg", Code
        assert not re.search(r"<\s*script|foreignObject|<\s*iframe|<\s*object|<\s*embed|javascript:|@import|<!ENTITY|<!DOCTYPE", T, re.I), Code
        assert not re.search(r"\son[a-z]+\s*=", T, re.I), Code
        assert all(H.startswith("#") for H in re.findall(r"href\s*=\s*[\"']([^\"']*)[\"']", T)), Code   # nothing outside the file
        assert not re.search(r"url\(\s*['\"]?(?!#)", T), Code
        Assert.append(Code)
    assert len(Assert) == len(Addressing.Countries)
    License = (Flags / "LICENSE").read_text(encoding="utf-8")
    assert "MIT" in License and "flag-icons 7.2.3" in (Flags / "README.md").read_text(encoding="utf-8")
    assert {P.stem.upper() for P in Flags.glob("*.svg")} == set(Addressing.CountryNames)      # no flag nobody uses


async def test_flags_are_served_as_images_of_the_site(H):
    R = await H.Client.get("/static/vendor/flags/il.svg")
    assert R.status_code == 200 and R.headers["content-type"].startswith("image/svg+xml")
    assert "url(`/static/vendor/flags/${String(cc || '').toLowerCase()}.svg`)" in App      # same origin, under the base path (CSP img-src 'self')


def test_the_pickers_are_comboboxes_with_listboxes_and_sized_flags():
    for Button, List, Kind in (("co-country", "co-country-list", "country"), ("co-phone-cc", "co-phone-cc-list", "dial")):
        Tag = re.search(r'<button type="button" id="' + Button + r'"[^>]*>', Index, re.S).group(0)
        for Attr in ('role="combobox"', 'aria-haspopup="listbox"', f'aria-controls="{List}"', ":aria-expanded=", ":aria-activedescendant=",
                     f"@keydown=\"pickerKey('{Kind}', $event)\""):
            assert Attr in Tag, (Button, Attr)
        Ul = re.search(r'<ul [^>]*id="' + List + r'"[^>]*>', Index).group(0)
        assert 'role="listbox"' in Ul and "x-cloak" in Ul
        Opts = Index[Index.index(Ul):Index.index("</ul>", Index.index(Ul))]
        assert 'role="option"' in Opts and ':aria-selected=' in Opts and f"@click=\"pickerChoose('{Kind}', c.code)\"" in Opts
    # the phone: its own label, the icon beside the number, the number keeps the id the error and the focus use (R-24)
    assert '<label class="flbl" for="co-phone">Phone</label>' in Index and 'class="phone-ico"' in Index
    Phone = re.search(r'<input id="co-phone"[^>]*>', Index, re.S).group(0)
    assert 'x-model="co.phoneNum"' in Phone and ":aria-describedby=\"coProblem('phone') ? 'co-phone-err' : null\"" in Phone
    assert "phone: 'co-phone'" in App and "country: 'co-country'" in App
    # every flag is an <img> of a fixed size (nothing moves when it arrives), with no words of its own
    Imgs = re.findall(r"<img [^>]*class=\"flag\"[^>]*>", Index)
    assert len(Imgs) == 4 and all('width="20" height="15"' in I and 'alt=""' in I for I in Imgs)
    # the keys of a select: arrows, Home/End, Page keys, letters, Enter/Space, Escape (back to the button), Tab
    Keys = App[App.index("pickerKey(kind, e) {"):App.index("// ── The checkout draft")] if "// ── The checkout draft" in App[App.index("pickerKey(kind, e) {"):] else App
    for K in ("'ArrowDown'", "'ArrowUp'", "'Home'", "'End'", "'PageDown'", "'PageUp'", "'Enter'", "'Escape'", "'Tab'"):
        assert K in Keys, K
