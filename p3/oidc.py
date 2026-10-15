"""Sign in with Google and with Apple: the official OpenID Connect authorization-code flows, validated on the server.

The browser is only redirected to the provider and back. The code is exchanged here, over TLS, with the client secret;
the ID token it returns is verified (signature against the provider's published keys, issuer, audience, expiry and the
nonce we issued) before a single claim is believed. Nothing the browser sends is trusted as an identity.

Each provider exposes the same two calls, so tests substitute a fake:
    AuthorizeUrl(RedirectUri, State, Nonce, Verifier) -> str
    async Complete(Code, RedirectUri, Verifier, Nonce, UserHint) -> Claims
"""

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

Logger = logging.getLogger("p3.oidc")

GoogleIssuers = ("https://accounts.google.com", "accounts.google.com")
AppleIssuer = "https://appleid.apple.com"
JwksTtlS = 3600


class OidcError(Exception):
    """The provider's answer could not be trusted (never shown in detail to the customer)."""


@dataclass(frozen=True)
class Claims:
    Subject: str
    Email: str
    EmailVerified: bool
    Name: str
    Hd: str = ""                      # Google Workspace domain, when the account belongs to one


def _Truthy(V) -> bool:
    return V is True or (isinstance(V, str) and V.strip().lower() == "true")


def Challenge(Verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(Verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


class _Jwks:
    def __init__(self, Url: str):
        self.Url, self._Keys, self._At = Url, {}, 0.0

    async def Key(self, Kid: str, Http: httpx.AsyncClient):
        if Kid not in self._Keys or time.monotonic() - self._At > JwksTtlS:
            R = await Http.get(self.Url)
            R.raise_for_status()
            self._Keys = {K["kid"]: jwt.PyJWK(K).key for K in R.json().get("keys", []) if "kid" in K}
            self._At = time.monotonic()
        if Kid not in self._Keys:
            raise OidcError("unknown signing key")
        return self._Keys[Kid]


async def _Verify(IdToken: str, Jwks: _Jwks, Http: httpx.AsyncClient, Audience: str, Issuers: tuple, Nonce: str) -> dict:
    try:
        Header = jwt.get_unverified_header(IdToken)
        if Header.get("alg") != "RS256":
            raise OidcError("unexpected signing algorithm")
        Key = await Jwks.Key(Header.get("kid", ""), Http)
        Data = jwt.decode(IdToken, Key, algorithms=["RS256"], audience=Audience, leeway=60,
                          options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except OidcError:
        raise
    except Exception as E:                                  # bad signature, expired, wrong audience, malformed, network
        raise OidcError(f"ID token rejected: {type(E).__name__}") from E
    if Data.get("iss") not in Issuers:
        raise OidcError("wrong issuer")
    if not Nonce or Data.get("nonce") != Nonce:
        raise OidcError("nonce mismatch")
    return Data


class GoogleProvider:
    Name = "google"
    AuthUrl = "https://accounts.google.com/o/oauth2/v2/auth"
    TokenUrl = "https://oauth2.googleapis.com/token"
    JwksUrl = "https://www.googleapis.com/oauth2/v3/certs"

    def __init__(self, ClientId: str, ClientSecret: str, Http: httpx.AsyncClient | None = None):
        self.ClientId, self.ClientSecret = ClientId, ClientSecret
        self.Http = Http or httpx.AsyncClient(timeout=10)
        self._Jwks = _Jwks(self.JwksUrl)

    def AuthorizeUrl(self, RedirectUri: str, State: str, Nonce: str, Verifier: str, Prompt: str = "select_account") -> str:
        return self.AuthUrl + "?" + urlencode({
            "client_id": self.ClientId, "redirect_uri": RedirectUri, "response_type": "code", "scope": "openid email profile",
            "state": State, "nonce": Nonce, "code_challenge": Challenge(Verifier), "code_challenge_method": "S256",
            "prompt": Prompt})

    async def Complete(self, Code: str, RedirectUri: str, Verifier: str, Nonce: str, UserHint: dict | None = None) -> Claims:
        try:
            R = await self.Http.post(self.TokenUrl, data={
                "code": Code, "client_id": self.ClientId, "client_secret": self.ClientSecret, "redirect_uri": RedirectUri,
                "grant_type": "authorization_code", "code_verifier": Verifier})
            R.raise_for_status()
            IdToken = R.json()["id_token"]
        except Exception as E:
            raise OidcError(f"token exchange failed: {type(E).__name__}") from E
        D = await _Verify(IdToken, self._Jwks, self.Http, self.ClientId, GoogleIssuers, Nonce)
        return Claims(Subject=str(D["sub"]), Email=str(D.get("email") or "").lower(), EmailVerified=_Truthy(D.get("email_verified")),
                      Name=str(D.get("name") or ""), Hd=str(D.get("hd") or ""))


class AppleProvider:
    """Sign in with Apple. The client secret is a short-lived JWT signed with the team's private key. Apple sends the
    person's name only the first time, as a JSON "user" form field next to the code (UserHint)."""
    Name = "apple"
    AuthUrl = "https://appleid.apple.com/auth/authorize"
    TokenUrl = "https://appleid.apple.com/auth/token"
    JwksUrl = "https://appleid.apple.com/auth/keys"

    def __init__(self, ClientId: str, TeamId: str, KeyId: str, PrivateKey: str, Http: httpx.AsyncClient | None = None):
        self.ClientId, self.TeamId, self.KeyId, self.PrivateKey = ClientId, TeamId, KeyId, PrivateKey
        self.Http = Http or httpx.AsyncClient(timeout=10)
        self._Jwks = _Jwks(self.JwksUrl)

    def ClientSecret(self) -> str:
        Now_ = int(time.time())
        return jwt.encode({"iss": self.TeamId, "iat": Now_, "exp": Now_ + 600, "aud": AppleIssuer, "sub": self.ClientId},
                          self.PrivateKey, algorithm="ES256", headers={"kid": self.KeyId})

    def AuthorizeUrl(self, RedirectUri: str, State: str, Nonce: str, Verifier: str, Prompt: str = "") -> str:
        return self.AuthUrl + "?" + urlencode({
            "client_id": self.ClientId, "redirect_uri": RedirectUri, "response_type": "code", "response_mode": "form_post",
            "scope": "name email", "state": State, "nonce": Nonce})

    async def Complete(self, Code: str, RedirectUri: str, Verifier: str, Nonce: str, UserHint: dict | None = None) -> Claims:
        try:
            R = await self.Http.post(self.TokenUrl, data={
                "code": Code, "client_id": self.ClientId, "client_secret": self.ClientSecret(), "redirect_uri": RedirectUri,
                "grant_type": "authorization_code"})
            R.raise_for_status()
            IdToken = R.json()["id_token"]
        except Exception as E:
            raise OidcError(f"token exchange failed: {type(E).__name__}") from E
        D = await _Verify(IdToken, self._Jwks, self.Http, self.ClientId, (AppleIssuer,), Nonce)
        Name = ""
        try:
            N = (UserHint or {}).get("name") or {}
            Name = " ".join(P for P in (N.get("firstName"), N.get("lastName")) if P)
        except AttributeError:
            pass
        return Claims(Subject=str(D["sub"]), Email=str(D.get("email") or "").lower(), EmailVerified=_Truthy(D.get("email_verified")),
                      Name=Name)


def BuildProviders(S) -> dict:
    """The providers that have credentials configured (an unconfigured one simply does not appear on the sign-in page)."""
    Out = {}
    if S.GoogleClientId and S.GoogleClientSecret:
        Out["google"] = GoogleProvider(S.GoogleClientId, S.GoogleClientSecret)
    if S.AppleClientId and S.AppleTeamId and S.AppleKeyId and S.ApplePrivateKey:
        Out["apple"] = AppleProvider(S.AppleClientId, S.AppleTeamId, S.AppleKeyId, S.ApplePrivateKey)
    return Out


def ParseUserHint(Raw: str | None) -> dict:
    try:
        V = json.loads(Raw or "{}")
        return V if isinstance(V, dict) else {}
    except ValueError:
        return {}
