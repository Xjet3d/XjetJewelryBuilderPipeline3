"""AJW-41: the real AppleProvider (p3/oidc.py) against a stand-in for appleid.apple.com — no network, no Apple account.

Keys are made in memory for each test (an EC P-256 key for the client secret, an RSA key for Apple's published keys);
nothing here is a credential and no key file is written."""

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from p3.oidc import AppleIssuer, AppleProvider, Claims, OidcError, ParseUserHint

ClientId, TeamId, KeyId, Kid, NonceValue = "test.services.id", "TEAM123456", "KEY1234567", "apple-kid-1", "nonce-abc"
Redirect = "https://site.test/api/auth/apple/callback"


def _Pem(Key) -> str:
    return Key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


@pytest.fixture(scope="module")
def Keys():
    Rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    Jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(Rsa.public_key()))
    Jwk.update({"kid": Kid, "alg": "RS256", "use": "sig"})
    return {"Rsa": Rsa, "Jwks": {"keys": [Jwk]}, "Ec": ec.generate_private_key(ec.SECP256R1())}


class Apple:
    """Answers /auth/keys and /auth/token the way Apple does, and remembers what the provider sent."""

    def __init__(self, Keys, Claim=None, TokenStatus=200, Alg="RS256", SignKid=Kid):
        self.Keys, self.TokenStatus, self.Alg, self.SignKid = Keys, TokenStatus, Alg, SignKid
        self.Claim = Claim if Claim is not None else {}
        self.Posted, self.KeyFetches = [], 0

    def IdToken(self):
        Now_ = int(time.time())
        Body = {"iss": AppleIssuer, "aud": ClientId, "sub": "001234.abcdef.5678", "iat": Now_, "exp": Now_ + 600, "nonce": NonceValue,
                "email": "Dana@Example.com", "email_verified": "true", "is_private_email": "false", **self.Claim}
        Body = {K: V for K, V in Body.items() if V is not None}
        Key = self.Keys["Rsa"] if self.Alg == "RS256" else "shared-secret-for-an-hs256-forgery-xx"
        return jwt.encode(Body, Key, algorithm=self.Alg, headers={"kid": self.SignKid})

    def __call__(self, Req: httpx.Request) -> httpx.Response:
        if Req.url.path == "/auth/keys":
            self.KeyFetches += 1
            return httpx.Response(200, json=self.Keys["Jwks"])
        if Req.url.path == "/auth/token":
            self.Posted.append({K: V[0] for K, V in parse_qs(Req.content.decode()).items()})
            if self.TokenStatus != 200:
                return httpx.Response(self.TokenStatus, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.IdToken(), "access_token": "x", "refresh_token": "y", "token_type": "Bearer"})
        return httpx.Response(404)


def Provider(Fake: Apple) -> AppleProvider:
    return AppleProvider(ClientId, TeamId, KeyId, _Pem(Fake.Keys["Ec"]), Http=httpx.AsyncClient(transport=httpx.MockTransport(Fake)))


async def Complete(Fake: Apple, Hint=None, Nonce=NonceValue) -> Claims:
    return await Provider(Fake).Complete("the-code", Redirect, "unused-verifier", Nonce, Hint)


# ── what we send to Apple ───────────────────────────────────────────────────

def test_the_client_secret_is_the_es256_jwt_apple_documents(Keys):
    P = Provider(Apple(Keys))
    Header = jwt.get_unverified_header(P.ClientSecret())
    assert (Header["alg"], Header["kid"]) == ("ES256", KeyId)
    Body = jwt.decode(P.ClientSecret(), Keys["Ec"].public_key(), algorithms=["ES256"], audience=AppleIssuer)
    assert (Body["iss"], Body["sub"], Body["aud"]) == (TeamId, ClientId, AppleIssuer)
    assert 0 < Body["exp"] - Body["iat"] <= 15777000           # Apple accepts at most six months; we keep it to minutes


def test_the_authorize_url_asks_for_a_form_post_with_name_and_email(Keys):
    Url = urlparse(Provider(Apple(Keys)).AuthorizeUrl(Redirect, "the-state", NonceValue, "verifier"))
    Q = {K: V[0] for K, V in parse_qs(Url.query).items()}
    assert f"{Url.scheme}://{Url.netloc}{Url.path}" == "https://appleid.apple.com/auth/authorize"
    assert Q == {"client_id": ClientId, "redirect_uri": Redirect, "response_type": "code", "response_mode": "form_post",
                 "scope": "name email", "state": "the-state", "nonce": NonceValue}


async def test_the_code_is_exchanged_with_the_signed_client_secret_and_no_pkce_verifier(Keys):
    Fake = Apple(Keys)
    await Complete(Fake)
    Sent = Fake.Posted[0]
    assert (Sent["grant_type"], Sent["code"], Sent["client_id"], Sent["redirect_uri"]) == ("authorization_code", "the-code", ClientId, Redirect)
    assert jwt.get_unverified_header(Sent["client_secret"])["kid"] == KeyId
    assert "code_verifier" not in Sent                         # Apple's token endpoint does not take one


# ── what we believe from Apple ──────────────────────────────────────────────

async def test_a_good_token_gives_the_stable_subject_and_a_lowercased_verified_email(Keys):
    C = await Complete(Apple(Keys))
    assert C == Claims(Subject="001234.abcdef.5678", Email="dana@example.com", EmailVerified=True, Name="")


async def test_the_name_comes_only_from_the_first_authorization_hint(Keys):
    Hint = ParseUserHint(json.dumps({"name": {"firstName": "Dana", "lastName": "Levi"}, "email": "dana@example.com"}))
    assert (await Complete(Apple(Keys), Hint)).Name == "Dana Levi"
    assert (await Complete(Apple(Keys), ParseUserHint(""))).Name == ""                       # a returning customer: Apple sends none
    for Junk in ("not json", "[1, 2]", '{"name": "Dana"}', '{"name": null}'):
        assert (await Complete(Apple(Keys), ParseUserHint(Junk))).Name == ""                  # a hint can never break the sign-in


async def test_a_private_relay_address_is_passed_through_for_the_account_rules_to_judge(Keys):
    C = await Complete(Apple(Keys, {"email": "x1y2z3@privaterelay.appleid.com", "is_private_email": "true"}))
    assert (C.Email, C.EmailVerified) == ("x1y2z3@privaterelay.appleid.com", True)


async def test_a_token_without_an_email_is_still_a_sign_in(Keys):                              # Apple omits it for a returning, non-first login
    C = await Complete(Apple(Keys, {"email": None, "email_verified": None}))
    assert (C.Subject, C.Email, C.EmailVerified) == ("001234.abcdef.5678", "", False)


@pytest.mark.parametrize("Claim", [{"email_verified": "false"}, {"email_verified": False}, {"email_verified": None}])
async def test_an_unverified_email_is_not_believed(Keys, Claim):
    assert (await Complete(Apple(Keys, Claim))).EmailVerified is False


@pytest.mark.parametrize("Claim, Nonce", [
    ({}, "another-nonce"),                                  # replayed from a different round trip
    ({"nonce": None}, NonceValue),                          # no nonce in the token
    ({}, ""),                                               # no nonce on our side
    ({"aud": "someone.else"}, NonceValue),                  # issued to another client
    ({"iss": "https://evil.example"}, NonceValue),
    ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, NonceValue),   # expired
    ({"sub": None}, NonceValue),                            # no subject
])
async def test_a_token_that_is_not_ours_is_refused(Keys, Claim, Nonce):
    with pytest.raises(OidcError):
        await Complete(Apple(Keys, Claim), Nonce=Nonce)


async def test_a_token_signed_by_an_unknown_key_or_a_forged_algorithm_is_refused(Keys):
    with pytest.raises(OidcError):
        await Complete(Apple(Keys, SignKid="not-apples"))
    with pytest.raises(OidcError, match="algorithm"):
        await Complete(Apple(Keys, Alg="HS256"))
    Other = {**Keys, "Rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048)}   # right kid, wrong private key
    with pytest.raises(OidcError):
        await Complete(Apple(Other | {"Jwks": Keys["Jwks"]}))


async def test_apple_refusing_the_code_is_a_failed_sign_in_not_a_crash(Keys):
    for Status in (400, 401, 500):
        with pytest.raises(OidcError, match="token exchange"):
            await Complete(Apple(Keys, TokenStatus=Status))


async def test_apples_published_keys_are_fetched_once_and_reused(Keys):
    Fake = Apple(Keys)
    P = Provider(Fake)
    for _ in range(3):
        await P.Complete("c", Redirect, "v", NonceValue)
    assert Fake.KeyFetches == 1


# ── configuration: a half-configured Apple says why, and never prints a value ───────────────────────

def _Settings(**Over):
    from types import SimpleNamespace
    return SimpleNamespace(**{"GoogleClientId": "", "GoogleClientSecret": "", "AppleClientId": "", "AppleTeamId": "", "AppleKeyId": "",
                              "ApplePrivateKey": "", **Over})


def test_apple_appears_only_with_all_four_settings_and_otherwise_names_what_is_missing(caplog):
    from p3.oidc import BuildProviders
    assert BuildProviders(_Settings()) == {} and not caplog.records                       # nothing configured: silence
    with caplog.at_level("WARNING", logger="p3.oidc"):
        assert "apple" not in BuildProviders(_Settings(AppleClientId="svc.id", AppleTeamId="TEAM123456"))
    Said = caplog.text
    assert "P3_APPLE_KEY_ID" in Said and "P3_APPLE_PRIVATE_KEY" in Said and "svc.id" not in Said and "TEAM123456" not in Said
    Full = _Settings(AppleClientId="svc.id", AppleTeamId="TEAM123456", AppleKeyId="KEY1234567", ApplePrivateKey="pem")
    assert isinstance(BuildProviders(Full)["apple"], AppleProvider)


def test_an_unreadable_private_key_path_is_reported_not_swallowed(monkeypatch, tmp_path, caplog):
    from p3.settings import _ApplePrivateKey
    monkeypatch.delenv("P3_APPLE_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("P3_APPLE_PRIVATE_KEY_PATH", str(tmp_path / "missing.p8"))
    with caplog.at_level("WARNING", logger="p3.settings"):
        assert _ApplePrivateKey() == ""
    assert "P3_APPLE_PRIVATE_KEY_PATH" in caplog.text and str(tmp_path) not in caplog.text
    (tmp_path / "k.p8").write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("P3_APPLE_PRIVATE_KEY_PATH", str(tmp_path / "k.p8"))
    assert _ApplePrivateKey().startswith("-----BEGIN PRIVATE KEY-----")
