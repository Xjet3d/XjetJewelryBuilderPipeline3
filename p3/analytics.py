"""Cookie consent and analytics (docs/ANALYTICS.md): what the customer pages may load, and only after consent.

Nothing optional runs before the visitor decides (web/consent.js). Analytics is configured in Admin → Settings →
System → Analytics & cookies: on or off, the one site host the tools may run on, the GA4 measurement ID and the
Microsoft Clarity project ID. Off — the default — or with no ID, a customer page gets no analytics configuration, no
consent banner is needed and the Content-Security-Policy stays as strict as before. So a copy of the site (proto, a
development machine) sends nothing unless it is configured for it, and an ID configured for one host cannot run on
another: the browser starts a tool only on the configured host.

Google Consent Mode, basic: the Google tag is not loaded at all until Analytics is accepted; then the four consent
signals start denied and only analytics_storage is granted (Atelier runs no advertising). Clarity loads only after
Analytics consent, told so through its consent API v2 (analytics granted, ads denied). The pages mask customer details
in recordings; the Admin and the developer tools never load either tool."""

import re

from p3.context import HttpError

Key = "analytics"                                   # product_settings (Admin → Settings → System), changes logged
Defaults = {"enabled": False, "host": "", "ga4": "", "clarity": ""}
ConsentVersion = 1                                  # web/consent.js asks again when this changes, or when a tool is added
Ga4Pattern = re.compile(r"G-[A-Z0-9]{4,20}")
ClarityPattern = re.compile(r"[a-z0-9]{6,20}")
HostPattern = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")

# The hosts each tool needs. Google: the Content Security Policy guide, Google Analytics "without any Ads features";
# Microsoft: the Clarity CSP guide (Clarity balances its collectors over several *.clarity.ms hosts).
GoogleSources = {"script-src": ("https://www.googletagmanager.com",),
                 "img-src": ("https://www.googletagmanager.com", "https://*.google-analytics.com"),
                 "connect-src": ("https://www.googletagmanager.com", "https://*.google-analytics.com", "https://*.google.com")}
ClaritySources = {"script-src": ("https://*.clarity.ms",),
                  "img-src": ("https://*.clarity.ms", "https://c.bing.com"),
                  "connect-src": ("https://*.clarity.ms", "https://c.bing.com")}


def Placeholder(Tool: str, Id: str | None) -> bool:
    """A functional-test ID (G-TEST… for GA4, test… for Clarity): web/consent.js runs the banner and the events but
    loads neither tool and sends nothing — and the page's policy does not open the tool's hosts either."""
    return bool(Id) and (Id.upper().startswith("G-TEST") if Tool == "ga4" else Id.lower().startswith("test"))


def Stored(Products) -> dict:
    """The saved configuration, with defaults for anything never set."""
    Raw = Products.Get(Key)
    return {**Defaults, **(Raw if isinstance(Raw, dict) else {})}


def Validate(Body: dict, Current: dict) -> dict:
    """The Admin's change, checked: a measurement ID looks like G-XXXXXXX, a Clarity project ID is its short
    lowercase code, the host is a bare host name (no scheme, path or port). Empty clears an ID."""
    if not isinstance(Body, dict) or not any(K in Body for K in Defaults):
        raise HttpError(400, "invalid_value", "Nothing to change.")
    Out = dict(Current)
    if "enabled" in Body:
        if not isinstance(Body["enabled"], bool):
            raise HttpError(400, "invalid_value", "enabled must be true or false.")
        Out["enabled"] = Body["enabled"]
    if "host" in Body:
        Host = str(Body.get("host") or "").strip().lower()
        Host = re.sub(r"^https?://", "", Host).split("/", 1)[0]
        if Host and not HostPattern.fullmatch(Host):
            raise HttpError(400, "invalid_host", "The site host is a host name only, e.g. xjetatelier.xjet3d.com (no https://, path or port).")
        Out["host"] = Host
    if "ga4" in Body:
        Id = str(Body.get("ga4") or "").strip().upper()
        if Id and not Ga4Pattern.fullmatch(Id):
            raise HttpError(400, "invalid_ga4", "A GA4 measurement ID looks like G-XXXXXXXXXX (Admin → Data streams in Google Analytics).")
        Out["ga4"] = Id
    if "clarity" in Body:
        Id = str(Body.get("clarity") or "").strip().lower()
        if Id and not ClarityPattern.fullmatch(Id):
            raise HttpError(400, "invalid_clarity", "A Clarity project ID is the short code in Settings → Overview of the Clarity project.")
        Out["clarity"] = Id
    if Out["enabled"] and not Out["host"]:
        raise HttpError(400, "host_required", "Set the site host the analytics may run on before turning analytics on.")
    return Out


def Public(Products) -> dict | None:
    """What a customer page may know (window.__p3.analytics): None unless analytics is on, a host is set and at least
    one tool has its ID — then nothing optional exists on the page and no banner is shown."""
    C = Stored(Products)
    if not (C["enabled"] and C["host"] and (C["ga4"] or C["clarity"])):
        return None
    return {"version": ConsentVersion, "host": C["host"], "ga4": C["ga4"] or None, "clarity": C["clarity"] or None}


def Csp(Base: str, P: dict | None) -> str:
    """The Content-Security-Policy for a customer page: the base policy, plus the hosts of each tool that is on."""
    if not P:
        return Base
    Extra: dict[str, list[str]] = {}
    for Tool, Sources in (("ga4", GoogleSources), ("clarity", ClaritySources)):
        if P.get(Tool) and not Placeholder(Tool, P[Tool]):
            for Directive, Hosts in Sources.items():
                Extra.setdefault(Directive, []).extend(H for H in Hosts if H not in Extra.get(Directive, []))
    Parts = []
    for Part in Base.split(";"):
        Words = Part.split()
        if Words and Words[0] in Extra:
            Words += [H for H in Extra.pop(Words[0]) if H not in Words]
        if Words:
            Parts.append(" ".join(Words))
    Parts += [f"{D} {' '.join(H)}" for D, H in Extra.items()]           # a directive the base did not name
    return "; ".join(Parts)
