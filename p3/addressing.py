"""Shipping addresses for international ordering, and the seam for an Address Validation API.

Nothing here is tied to one provider. `AddressValidator` is a tiny protocol; the default `NoValidator`
leaves every address `unverified`. A real provider returns one of
    unverified · verified · corrected · failed
and, for `corrected`, a suggested address that the CUSTOMER confirms — their own wording is never
replaced silently. The order keeps the status and the suggestion it was placed with.
"""

import os
import re
from typing import Protocol

# ISO 3166-1 alpha-2 code → name. Shipping is worldwide (duties and VAT paid by the recipient).
Countries = [
    ("US", "United States"), ("CA", "Canada"), ("GB", "United Kingdom"), ("IE", "Ireland"), ("IL", "Israel"),
    ("DE", "Germany"), ("FR", "France"), ("IT", "Italy"), ("ES", "Spain"), ("PT", "Portugal"), ("NL", "Netherlands"),
    ("BE", "Belgium"), ("LU", "Luxembourg"), ("CH", "Switzerland"), ("AT", "Austria"), ("DK", "Denmark"), ("SE", "Sweden"),
    ("NO", "Norway"), ("FI", "Finland"), ("IS", "Iceland"), ("PL", "Poland"), ("CZ", "Czechia"), ("SK", "Slovakia"),
    ("HU", "Hungary"), ("RO", "Romania"), ("BG", "Bulgaria"), ("GR", "Greece"), ("CY", "Cyprus"), ("MT", "Malta"),
    ("HR", "Croatia"), ("SI", "Slovenia"), ("EE", "Estonia"), ("LV", "Latvia"), ("LT", "Lithuania"), ("TR", "Türkiye"),
    ("AE", "United Arab Emirates"), ("SA", "Saudi Arabia"), ("QA", "Qatar"), ("KW", "Kuwait"), ("BH", "Bahrain"),
    ("OM", "Oman"), ("JO", "Jordan"), ("EG", "Egypt"), ("ZA", "South Africa"), ("MA", "Morocco"), ("KE", "Kenya"),
    ("NG", "Nigeria"), ("IN", "India"), ("SG", "Singapore"), ("MY", "Malaysia"), ("TH", "Thailand"), ("VN", "Vietnam"),
    ("ID", "Indonesia"), ("PH", "Philippines"), ("JP", "Japan"), ("KR", "South Korea"), ("TW", "Taiwan"),
    ("HK", "Hong Kong"), ("CN", "China"), ("AU", "Australia"), ("NZ", "New Zealand"), ("MX", "Mexico"),
    ("BR", "Brazil"), ("AR", "Argentina"), ("CL", "Chile"), ("CO", "Colombia"), ("PE", "Peru"),
]
CountryNames = dict(Countries)
# Each shipping country's international dialling code: Checkout's phone field picks it beside the number (with the
# country's flag, web/vendor/flags/<code>.svg), and the number is sent as "+972 54 123 4567"
DialCodes = {
    "US": "+1", "CA": "+1", "GB": "+44", "IE": "+353", "IL": "+972", "DE": "+49", "FR": "+33", "IT": "+39", "ES": "+34",
    "PT": "+351", "NL": "+31", "BE": "+32", "LU": "+352", "CH": "+41", "AT": "+43", "DK": "+45", "SE": "+46", "NO": "+47",
    "FI": "+358", "IS": "+354", "PL": "+48", "CZ": "+420", "SK": "+421", "HU": "+36", "RO": "+40", "BG": "+359", "GR": "+30",
    "CY": "+357", "MT": "+356", "HR": "+385", "SI": "+386", "EE": "+372", "LV": "+371", "LT": "+370", "TR": "+90",
    "AE": "+971", "SA": "+966", "QA": "+974", "KW": "+965", "BH": "+973", "OM": "+968", "JO": "+962", "EG": "+20",
    "ZA": "+27", "MA": "+212", "KE": "+254", "NG": "+234", "IN": "+91", "SG": "+65", "MY": "+60", "TH": "+66", "VN": "+84",
    "ID": "+62", "PH": "+63", "JP": "+81", "KR": "+82", "TW": "+886", "HK": "+852", "CN": "+86", "AU": "+61", "NZ": "+64",
    "MX": "+52", "BR": "+55", "AR": "+54", "CL": "+56", "CO": "+57", "PE": "+51",
}
# Where a number's leading 0 stays after the dialling code (Italy: +39 06 …); elsewhere it is the national prefix that
# the international form drops (054 … in Israel is +972 54 …)
KeepsLeadingZero = {"IT"}


def CountryList() -> list[dict]:
    """The shipping countries as Checkout offers them: code, name, dialling code, and whether a leading 0 stays."""
    return [{"code": C, "name": N, "dial": DialCodes[C], **({"keep_zero": True} if C in KeepsLeadingZero else {})}
            for C, N in Countries]
RegionRequired = {"US", "CA", "AU", "BR", "MX", "IN", "CN"}          # state / province / prefecture
NoPostalCode = {"AE", "HK", "QA", "BH"}                              # countries that do not use postal codes
PostalPatterns = {
    "US": r"^\d{5}(-\d{4})?$", "CA": r"^[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d$",
    "GB": r"^[A-Za-z]{1,2}\d[A-Za-z\d]?\s?\d[A-Za-z]{2}$", "IE": r"^[A-Za-z]\d{2}\s?[A-Za-z\d]{4}$",
    "IL": r"^\d{5}(\d{2})?$", "DE": r"^\d{5}$", "FR": r"^\d{5}$", "IT": r"^\d{5}$", "ES": r"^\d{5}$",
    "AU": r"^\d{4}$", "NZ": r"^\d{4}$", "JP": r"^\d{3}-?\d{4}$", "SG": r"^\d{6}$", "IN": r"^\d{6}$",
    "CH": r"^\d{4}$", "AT": r"^\d{4}$", "NL": r"^\d{4}\s?[A-Za-z]{2}$", "BR": r"^\d{5}-?\d{3}$",
}
Fields = ("recipient", "line1", "line2", "city", "region", "postal_code", "country")


def Normalize(Raw: dict | None) -> dict:
    Raw = Raw or {}
    Out = {K: str(Raw.get(K) or "").strip() for K in Fields}
    Out["country"] = Out["country"].upper()
    Out["postal_code"] = Out["postal_code"].upper()
    return Out


def Problems(Address: dict) -> list[dict]:
    """Field-level problems ({field, message}); empty when the address is complete for its country."""
    A, Out = Normalize(Address), []
    if not A["recipient"]:
        Out.append({"field": "recipient", "message": "Please enter the recipient's name."})
    if len(A["line1"]) < 3:
        Out.append({"field": "line1", "message": "Please enter the street address."})
    if not A["city"]:
        Out.append({"field": "city", "message": "Please enter the city."})
    if A["country"] not in CountryNames:
        Out.append({"field": "country", "message": "Please choose a country."})
        return Out
    if A["country"] in RegionRequired and not A["region"]:
        Out.append({"field": "region", "message": "Please enter the state, province or region."})
    if A["country"] not in NoPostalCode:
        if not A["postal_code"]:
            Out.append({"field": "postal_code", "message": "Please enter the postal code."})
        elif A["country"] in PostalPatterns and not re.match(PostalPatterns[A["country"]], A["postal_code"]):
            Out.append({"field": "postal_code", "message": f"That does not look like a {CountryNames[A['country']]} postal code."})
    return Out


def Lines(Address: dict) -> list[str]:
    """Display lines (also used in the confirmation email)."""
    A = Normalize(Address)
    CityLine = " ".join(P for P in (A["city"], A["region"], A["postal_code"]) if P)
    return [L for L in (A["recipient"], A["line1"], A["line2"], CityLine, CountryNames.get(A["country"], A["country"])) if L]


class AddressValidator(Protocol):
    Name: str

    def Check(self, Address: dict) -> dict:
        """{"status": unverified|verified|corrected|failed, "suggestion": address|None, "message": str}"""


class NoValidator:
    """No external service connected: the address is stored exactly as the customer entered it."""
    Name = "none"

    def Check(self, Address: dict) -> dict:
        return {"status": "unverified", "suggestion": None,
                "message": "Address validation is not connected yet; the address is stored as entered."}


def BuildValidator() -> AddressValidator:
    Kind = os.environ.get("P3_ADDRESS_VALIDATOR", "none").strip().lower()
    if Kind not in ("", "none"):
        raise ValueError(f"Unknown address validator {Kind!r} (only 'none' is implemented yet)")
    return NoValidator()
