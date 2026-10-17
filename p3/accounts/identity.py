"""Customer login identities, passwords, single-use tokens and sessions (AJW-39).

Lives inside the accounts package, in accounts.db next to the accounts it authenticates: nothing outside
p3/accounts touches these tables. A customer account keeps the id it always had ("p3local:acct_…"), so designs,
credits, quotes and orders are never touched; this module only adds ways to prove who the account is.

* login_identities   one row per way in: google / apple (provider + subject) or password (subject = the account id)
* password_credentials  argon2id hashes
* auth_tokens        verify-email, reset-password, confirm-link and legacy-claim secrets: random, stored as SHA-256,
                     single use, time limited
* customer_sessions  opaque random session ids, stored as SHA-256, revocable, with the time of the last real sign-in
* oauth_states       one row per Google / Apple round trip: state, nonce and PKCE verifier

Welcome credits are granted by exactly one code path (CreateAccount). Linking, claiming and migrating never call it.
"""

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from p3.accounts import Principal
from p3.db import Now

Providers = ("google", "apple")
SessionIdleDays = 30
SessionAbsoluteDays = 180
SessionTouchMinutes = 30
RecentAuthMinutes = 10
TokenTtlMinutes = {"verify_email": 24 * 60, "reset_password": 60, "confirm_link": 60, "claim": 30}
PasswordMinLength = 10
PasswordMaxLength = 128

# argon2id at the OWASP minimum (19 MiB, 2 passes, 1 lane): ~25 ms per hash on a server CPU
_Hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_DummyHash = _Hasher.hash("not-a-real-password-for-timing")

# A short list of passwords that satisfy the length rule and are still among the first a script tries
_Common = {"password12", "password123", "1234567890", "12345678910", "qwertyuiop", "qwerty1234", "iloveyou12",
           "abcdefghij", "1q2w3e4r5t", "0123456789", "letmein123", "welcome123", "administrator", "passw0rd123",
           "password1!", "xjetatelier", "atelier123"}

SessionCookieMark = "s1."          # the prefix the HTTP layer puts on a session id it hands to Authenticate


Schema = """
CREATE TABLE IF NOT EXISTS login_identities (
    id             TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    provider       TEXT NOT NULL CHECK (provider IN ('google', 'apple', 'password')),
    subject        TEXT NOT NULL,            -- the provider's stable user id; for 'password', the account id
    email          TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    display_name   TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    last_used_at   TEXT,
    UNIQUE (provider, subject)
);
CREATE INDEX IF NOT EXISTS login_identities_account ON login_identities(account_id);
CREATE UNIQUE INDEX IF NOT EXISTS login_identities_password_email ON login_identities(lower(email)) WHERE provider = 'password';

CREATE TABLE IF NOT EXISTS password_credentials (
    account_id  TEXT PRIMARY KEY,
    hash        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    id          TEXT PRIMARY KEY,
    purpose     TEXT NOT NULL,
    token_digest TEXT NOT NULL UNIQUE,       -- sha256 of the secret; the secret itself is only ever e-mailed or shown once
    account_id  TEXT NOT NULL,
    email       TEXT,
    data_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE TABLE IF NOT EXISTS customer_sessions (
    id                TEXT PRIMARY KEY,
    session_digest    TEXT NOT NULL UNIQUE,
    csrf              TEXT NOT NULL,
    account_id        TEXT NOT NULL,
    auth_method       TEXT NOT NULL,         -- password | google | apple | claim | verify | reset
    auth_at           TEXT NOT NULL,         -- the last real sign-in (password / provider), for "recent authentication"
    created_at        TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    expires_at        TEXT NOT NULL,         -- idle expiry, slides
    absolute_expires_at TEXT NOT NULL,
    user_agent        TEXT,
    revoked_at        TEXT,
    revoked_reason    TEXT
);
CREATE INDEX IF NOT EXISTS customer_sessions_account ON customer_sessions(account_id);

CREATE TABLE IF NOT EXISTS oauth_states (
    state_digest TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    nonce        TEXT NOT NULL,
    verifier     TEXT NOT NULL,
    purpose      TEXT NOT NULL,              -- login | link | claim | reauth | admin
    account_id   TEXT,
    target       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    used_at      TEXT
);
"""


class IdentityError(Exception):
    """A refusal the customer may be told about (Code is stable, Message is customer-safe)."""

    def __init__(self, Code: str, Message: str):
        super().__init__(Message)
        self.Code, self.Message = Code, Message


def _Utc() -> datetime:
    return datetime.now(timezone.utc)


def _Iso(Dt: datetime) -> str:
    return Dt.isoformat(timespec="milliseconds")


def _Digest(Purpose: str, Secret: str) -> str:
    return hashlib.sha256(f"{Purpose}:{Secret}".encode("utf-8")).hexdigest()


def NormalizeEmail(Email: str | None) -> str:
    return (Email or "").strip().lower()


EmailPattern = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UsernamePattern = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")


def IsRelayEmail(Email: str | None) -> bool:
    """An Apple "Hide My Email" relay address: it forwards to the person but matches no address we hold."""
    return NormalizeEmail(Email).endswith("@privaterelay.appleid.com")


def PasswordProblem(Password: str, Email: str = "") -> str | None:
    """A customer-safe reason the password is refused, or None."""
    if not isinstance(Password, str) or len(Password) < PasswordMinLength:
        return f"Use at least {PasswordMinLength} characters."
    if len(Password) > PasswordMaxLength:
        return f"Use at most {PasswordMaxLength} characters."
    Low = Password.lower()
    if Low in _Common or len(set(Low)) < 4:
        return "That password is too easy to guess. Choose a longer or less common one."
    Local = NormalizeEmail(Email).split("@")[0]
    if Local and len(Local) >= 4 and Low == Local:
        return "The password must not be your email address."
    return None


class IdentityStore:
    def __init__(self, Accounts):
        self.Accounts = Accounts                   # the LocalAccountProvider that owns the Db
        self.Db = Accounts.Db
        with self.Db.Connect() as Conn:
            Conn.executescript(Schema)
        with self.Db.Transaction() as Conn:
            Existing = {R[1] for R in Conn.execute("PRAGMA table_info(accounts)")}
            if "claimed_at" not in Existing:
                Conn.execute("ALTER TABLE accounts ADD COLUMN claimed_at TEXT")   # the six-letter code was retired for a real login

    # ── accounts ─────────────────────────────────────────────────────────
    def _Account(self, AccountId: str) -> dict | None:
        return self.Db.One("SELECT * FROM accounts WHERE account_id = ?", (AccountId,))

    Account = _Account

    def PasswordIdentity(self, Email: str) -> dict | None:
        """The password login whose sign-in address is this email: {account_id, email_verified}, or None."""
        return self.Db.One("SELECT account_id, email_verified FROM login_identities WHERE provider = 'password' AND lower(email) = ?",
                           (NormalizeEmail(Email),))

    def HasVerifiedPassword(self, AccountId: str) -> bool:
        return bool(self.Db.One("SELECT 1 AS x FROM login_identities WHERE account_id = ? AND provider = 'password' AND email_verified = 1", (AccountId,)))

    def Event(self, AccountId: str, Kind: str, Detail: str = "") -> None:
        self.Accounts._Event(AccountId, Kind, Detail)

    def AccountsByEmail(self, Email: str) -> list[dict]:
        """Every account with this address, live ones first, then the one the customer really used."""
        return self.Db.All("SELECT * FROM accounts WHERE lower(email) = ? ORDER BY (removed_at IS NOT NULL), "
                           "(status != 'active'), (last_sign_in_at IS NULL), last_sign_in_at DESC, generations_used DESC, "
                           "created_at ASC", (NormalizeEmail(Email),))

    def LiveAccountByEmail(self, Email: str) -> dict | None:
        for A in self.AccountsByEmail(Email):
            if not A["removed_at"] and A["status"] == "active":
                return A
        return None

    def Blocked(self, Email: str) -> str | None:
        """'removed' / 'disabled' when this address belongs only to accounts XJet switched off or removed."""
        Rows = self.AccountsByEmail(Email)
        if not Rows or any(not A["removed_at"] and A["status"] == "active" for A in Rows):
            return None
        return "removed" if all(A["removed_at"] for A in Rows) else "disabled"

    def CreateAccount(self, Name: str, Email: str | None, EmailVerified: bool) -> str:
        """The only place a login-created account (and its welcome credits) comes into being."""
        from p3.accounts.local import DefaultMaxGenerations, NewAccountId
        AccountId, T = NewAccountId(), Now()
        self.Db.Execute("INSERT INTO accounts (account_id, display_name, email, created_at, source, max_generations, "
                        "verified_at, activated_at) VALUES (?,?,?,?, 'auth', ?,?,?)",
                        (AccountId, (Name or "").strip(), NormalizeEmail(Email) or None, T, DefaultMaxGenerations,
                         T if EmailVerified else None, T))
        self.Accounts._Event(AccountId, "account_created", "login")
        return AccountId

    def EmailVerified(self, AccountId: str) -> bool:
        A = self._Account(AccountId)
        if not A or not A["email"]:
            return False
        if A["verified_at"] and A["source"] != "admin":
            return True
        return bool(self.Db.One("SELECT 1 AS x FROM login_identities WHERE account_id = ? AND email_verified = 1 "
                                "AND lower(email) = ?", (AccountId, NormalizeEmail(A["email"]))))

    def Usable(self, AccountId: str) -> bool:
        A = self._Account(AccountId)
        return bool(A) and not A["removed_at"] and A["status"] == "active"

    def _Retire(self, AccountId: str) -> None:
        """The first real login on a legacy account retires its six-letter code: it is never a login again."""
        T = Now()
        self.Db.Execute("UPDATE access_tokens SET active = 0 WHERE account_id = ?", (AccountId,))
        self.Db.Execute("UPDATE accounts SET claimed_at = COALESCE(claimed_at, ?) WHERE account_id = ? AND source != 'auth'",
                        (T, AccountId))

    # ── login identities ─────────────────────────────────────────────────
    def Identities(self, AccountId: str) -> list[dict]:
        return self.Db.All("SELECT id, provider, email, email_verified, display_name, created_at, last_used_at "
                           "FROM login_identities WHERE account_id = ? ORDER BY created_at", (AccountId,))

    def _Identity(self, Provider: str, Subject: str) -> dict | None:
        return self.Db.One("SELECT * FROM login_identities WHERE provider = ? AND subject = ?", (Provider, Subject))

    Identity = _Identity

    def _Attach(self, AccountId: str, Provider: str, Subject: str, Email: str | None, Verified: bool, Name: str) -> str:
        Id = "lid_" + secrets.token_hex(8)
        T = Now()
        self.Db.Execute("INSERT INTO login_identities (id, account_id, provider, subject, email, email_verified, display_name, "
                        "created_at, last_used_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (Id, AccountId, Provider, Subject, NormalizeEmail(Email) or None, 1 if Verified else 0,
                         (Name or "").strip(), T, T))
        A = self._Account(AccountId)
        if A and Verified and NormalizeEmail(Email) == NormalizeEmail(A["email"]):
            self.Db.Execute("UPDATE accounts SET verified_at = COALESCE(verified_at, ?) WHERE account_id = ?", (T, AccountId))
        if A and not A["display_name"] and Name:
            self.Db.Execute("UPDATE accounts SET display_name = ? WHERE account_id = ?", (Name.strip(), AccountId))
        if A and not A["email"] and Email and Verified and not IsRelayEmail(Email):
            self.Db.Execute("UPDATE accounts SET email = ? WHERE account_id = ?", (NormalizeEmail(Email), AccountId))
        if Verified:
            self._Retire(AccountId)
        self.Accounts._Event(AccountId, "login_connected", Provider, T)
        return Id

    def _DropUnverifiedPassword(self, AccountId: str) -> None:
        """A verified identity arrived for an address whose password registration was never confirmed: that password was
        chosen by whoever typed the address first, not necessarily by its owner. It is discarded."""
        if self.Db.One("SELECT 1 AS x FROM login_identities WHERE account_id = ? AND provider = 'password' AND email_verified = 0",
                       (AccountId,)):
            self.Db.Execute("DELETE FROM login_identities WHERE account_id = ? AND provider = 'password' AND email_verified = 0",
                            (AccountId,))
            self.Db.Execute("DELETE FROM password_credentials WHERE account_id = ?", (AccountId,))

    def HasMethod(self, AccountId: str, Excluding: str | None = None) -> bool:
        """Another way to sign in remains: a provider identity, or a password whose email is verified."""
        for I in self.Identities(AccountId):
            if I["id"] == Excluding:
                continue
            if I["provider"] in Providers or (I["provider"] == "password" and I["email_verified"]):
                return True
        return False

    def Unlink(self, AccountId: str, IdentityId: str) -> None:
        Row = self.Db.One("SELECT * FROM login_identities WHERE id = ? AND account_id = ?", (IdentityId, AccountId))
        if Row is None:
            raise IdentityError("not_found", "That sign-in method is not connected.")
        if not self.HasMethod(AccountId, Excluding=IdentityId):
            raise IdentityError("last_method", "This is your only way to sign in. Connect another one first.")
        self.Db.Execute("DELETE FROM login_identities WHERE id = ?", (IdentityId,))
        if Row["provider"] == "password":
            self.Db.Execute("DELETE FROM password_credentials WHERE account_id = ?", (AccountId,))
        self.Accounts._Event(AccountId, "login_disconnected", Row["provider"])

    # ── provider (Google / Apple) sign-in ────────────────────────────────
    def SignInWithProvider(self, Provider: str, Subject: str, Email: str | None, EmailVerified: bool, Name: str) -> dict:
        """{"status": "ok", "account_id", "created"} | {"status": "confirm", "account_id", "email"} (a verification mail
        must be sent) | {"status": "removed" | "disabled"}.

        An identity already known signs in. A new one whose provider vouches for the address links to the account that
        has that address (never a second account, never a second welcome credit). One the provider does not vouch for
        (unverified, or an Apple relay address) and that matches an account is held until the address's owner confirms
        by e-mail. Otherwise a new account is created."""
        Known = self._Identity(Provider, Subject)
        Email = NormalizeEmail(Email)
        if Known:
            if not self.Usable(Known["account_id"]):
                return {"status": self._Why(Known["account_id"])}
            self.Db.Execute("UPDATE login_identities SET last_used_at = ? WHERE id = ?", (Now(), Known["id"]))
            return {"status": "ok", "account_id": Known["account_id"], "created": False}
        Relay = IsRelayEmail(Email)
        if Email and not Relay:
            Match = self.LiveAccountByEmail(Email)
            if Match and EmailVerified:
                self._DropUnverifiedPassword(Match["account_id"])
                self._Attach(Match["account_id"], Provider, Subject, Email, True, Name)
                return {"status": "ok", "account_id": Match["account_id"], "created": False}
            if Match:
                return {"status": "confirm", "account_id": Match["account_id"], "email": Email}
            if self.Blocked(Email):
                return {"status": self.Blocked(Email)}
        AccountId = self.CreateAccount(Name or (Email.split("@")[0] if Email and not Relay else ""),
                                       None if Relay else Email, EmailVerified and not Relay)
        self._Attach(AccountId, Provider, Subject, Email, EmailVerified, Name)
        return {"status": "ok", "account_id": AccountId, "created": True}

    def _Why(self, AccountId: str) -> str:
        A = self._Account(AccountId)
        return "removed" if A and A["removed_at"] else "disabled"

    def LinkProvider(self, AccountId: str, Provider: str, Subject: str, Email: str | None, EmailVerified: bool, Name: str) -> str:
        """Connect a provider identity the signed-in customer just proved, to that account."""
        Known = self._Identity(Provider, Subject)
        if Known and Known["account_id"] == AccountId:
            return Known["id"]
        if Known:
            raise IdentityError("already_linked", "That account is already connected to a different XJet Atelier account.")
        if any(I["provider"] == Provider for I in self.Identities(AccountId)):
            raise IdentityError("provider_present", f"A different {Provider.title()} account is already connected. Disconnect it first.")
        return self._Attach(AccountId, Provider, Subject, Email, EmailVerified, Name)

    def ConfirmLink(self, AccountId: str, Provider: str, Subject: str, Email: str | None, Name: str) -> None:
        """The owner of the address confirmed (by e-mail) a provider identity the provider did not vouch for."""
        Known = self._Identity(Provider, Subject)
        if Known and Known["account_id"] != AccountId:
            raise IdentityError("already_linked", "That account is already connected to a different XJet Atelier account.")
        if not Known:
            self._DropUnverifiedPassword(AccountId)
            self._Attach(AccountId, Provider, Subject, Email, True, Name)

    def ClaimWithProvider(self, AccountId: str, Provider: str, Subject: str, Email: str | None, EmailVerified: bool, Name: str) -> None:
        """A legacy-code holder attaches a provider identity to the account that code opened."""
        self.LinkProvider(AccountId, Provider, Subject, Email, EmailVerified, Name)
        self._Retire(AccountId)                       # possession of the code is the proof; the code is spent

    # ── password ─────────────────────────────────────────────────────────
    def HashPassword(self, Password: str) -> str:
        return _Hasher.hash(Password)

    def SetPassword(self, AccountId: str, Password: str, Email: str, EmailVerified: bool, Hash: str | None = None) -> None:
        """Create or replace the account's password. The identity's address is the sign-in name. Hash: the argon2id hash of
        Password when the caller already made it."""
        Hash, T = Hash or self.HashPassword(Password), Now()
        Email = NormalizeEmail(Email)
        Row = self.Db.One("SELECT * FROM login_identities WHERE account_id = ? AND provider = 'password'", (AccountId,))
        Other = self.Db.One("SELECT account_id FROM login_identities WHERE provider = 'password' AND lower(email) = ?", (Email,))
        if Other and Other["account_id"] != AccountId:
            raise IdentityError("email_in_use", "That email already has a password login.")
        with self.Db.Transaction() as Conn:
            Conn.execute("INSERT INTO password_credentials (account_id, hash, updated_at) VALUES (?,?,?) "
                         "ON CONFLICT(account_id) DO UPDATE SET hash = excluded.hash, updated_at = excluded.updated_at",
                         (AccountId, Hash, T))
        if Row is None:
            self._Attach(AccountId, "password", AccountId, Email, EmailVerified, "")
        else:
            self.Db.Execute("UPDATE login_identities SET email = ?, email_verified = ? WHERE id = ?",
                            (Email, 1 if (EmailVerified or Row["email_verified"] and Row["email"] == Email) else 0, Row["id"]))
            if EmailVerified:
                self.MarkPasswordEmailVerified(AccountId)
        self.Accounts._Event(AccountId, "password_set", "")

    def SetUsernamePassword(self, Username: str, Password: str, Name: str = "") -> tuple[str, bool]:
        """Operator-made login by a plain username (no email, no mail ever sent to it): (account_id, created). The username is
        stored as the password identity's sign-in name, so VerifyPassword and the rate limits treat it like an address. It has no
        "@", so it can never equal an address a customer can register, and self-registration cannot make one."""
        Username = NormalizeEmail(Username)
        if not UsernamePattern.match(Username):
            raise IdentityError("invalid_username", "A username is 2-40 letters, digits, dots, dashes or underscores, with no @.")
        Problem = PasswordProblem(Password, Username)
        if Problem:
            raise IdentityError("weak_password", Problem)
        Row = self.PasswordIdentity(Username)
        Created = Row is None
        AccountId = Row["account_id"] if Row else self.CreateAccount(Name or Username, None, True)
        self.SetPassword(AccountId, Password, Username, False)      # False: "verified" would copy the name into accounts.email
        self.Db.Execute("UPDATE login_identities SET email_verified = 1 WHERE account_id = ? AND provider = 'password'", (AccountId,))
        self.Accounts._Event(AccountId, "username_login_set", "")
        return AccountId, Created

    def VerifyPassword(self, Email: str, Password: str) -> dict:
        """{"account_id", "verified"} for a right password, else raises IdentityError('invalid'). Unknown addresses cost the
        same time as wrong passwords, and the answer is the same."""
        Row = self.Db.One("SELECT i.account_id, i.email_verified, p.hash FROM login_identities i JOIN password_credentials p "
                          "ON p.account_id = i.account_id WHERE i.provider = 'password' AND lower(i.email) = ?",
                          (NormalizeEmail(Email),))
        Hash = Row["hash"] if Row else _DummyHash
        try:
            _Hasher.verify(Hash, Password or "")
            Right = Row is not None
        except (VerificationError, InvalidHashError):
            Right = False
        if not Right:
            raise IdentityError("invalid", "The email or password is not right.")
        if Row and _Hasher.check_needs_rehash(Row["hash"]):
            self.Db.Execute("UPDATE password_credentials SET hash = ?, updated_at = ? WHERE account_id = ?",
                            (self.HashPassword(Password), Now(), Row["account_id"]))
        return {"account_id": Row["account_id"], "verified": bool(Row["email_verified"])}

    def CheckPassword(self, AccountId: str, Password: str) -> bool:
        Row = self.Db.One("SELECT hash FROM password_credentials WHERE account_id = ?", (AccountId,))
        try:
            _Hasher.verify(Row["hash"] if Row else _DummyHash, Password or "")
            return Row is not None
        except (VerificationError, InvalidHashError):
            return False

    def MarkPasswordEmailVerified(self, AccountId: str) -> None:
        T = Now()
        Pw = self.Db.One("SELECT email FROM login_identities WHERE account_id = ? AND provider = 'password'", (AccountId,))
        self.Db.Execute("UPDATE login_identities SET email_verified = 1 WHERE account_id = ? AND provider = 'password'", (AccountId,))
        A = self._Account(AccountId)
        if A and Pw and not A["email"]:
            self.Db.Execute("UPDATE accounts SET email = ? WHERE account_id = ?", (Pw["email"], AccountId))
            A = self._Account(AccountId)
        if A and Pw and NormalizeEmail(A["email"]) == NormalizeEmail(Pw["email"]):
            self.Db.Execute("UPDATE accounts SET verified_at = COALESCE(verified_at, ?) WHERE account_id = ?", (T, AccountId))
        self._Retire(AccountId)

    # ── single-use tokens ────────────────────────────────────────────────
    def NewToken(self, Purpose: str, AccountId: str, Email: str | None = None, Data: dict | None = None) -> str:
        """A fresh secret (returned once, only its digest is kept). Earlier unused tokens of the same purpose for the
        account are voided, so only the latest link works."""
        Secret = secrets.token_urlsafe(32)
        T = _Utc()
        self.Db.Execute("UPDATE auth_tokens SET used_at = ? WHERE account_id = ? AND purpose = ? AND used_at IS NULL",
                        (_Iso(T), AccountId, Purpose))
        self.Db.Execute("INSERT INTO auth_tokens (id, purpose, token_digest, account_id, email, data_json, created_at, expires_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        ("atk_" + secrets.token_hex(8), Purpose, _Digest(Purpose, Secret), AccountId,
                         NormalizeEmail(Email) or None, json.dumps(Data or {}), _Iso(T),
                         _Iso(T + timedelta(minutes=TokenTtlMinutes[Purpose]))))
        return Secret

    def PeekToken(self, Purpose: str, Secret: str) -> dict | None:
        """The live token row (not used, not expired) without using it."""
        if not Secret or len(Secret) > 200:
            return None
        Row = self.Db.One("SELECT * FROM auth_tokens WHERE purpose = ? AND token_digest = ?", (Purpose, _Digest(Purpose, Secret.strip())))
        if Row is None or Row["used_at"] or Row["expires_at"] < _Iso(_Utc()):
            return None
        return {**Row, "data": json.loads(Row["data_json"] or "{}")}

    def UseToken(self, Purpose: str, Secret: str) -> dict | None:
        """Atomically consume a token: the first caller gets its row, any other gets None."""
        Row = self.PeekToken(Purpose, Secret)
        if Row is None:
            return None
        if not self.Db.Execute("UPDATE auth_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL", (Now(), Row["id"])):
            return None
        return Row

    def TokenState(self, Purpose: str, Secret: str) -> str:
        """'valid' | 'used' | 'expired' | 'invalid' — for the page a link opens (no detail beyond that)."""
        if not Secret or len(Secret) > 200:
            return "invalid"
        Row = self.Db.One("SELECT used_at, expires_at FROM auth_tokens WHERE purpose = ? AND token_digest = ?",
                          (Purpose, _Digest(Purpose, Secret.strip())))
        if Row is None:
            return "invalid"
        if Row["used_at"]:
            return "used"
        return "expired" if Row["expires_at"] < _Iso(_Utc()) else "valid"

    # ── OAuth round trips ────────────────────────────────────────────────
    def NewOAuthState(self, Provider: str, Purpose: str, AccountId: str | None = None, Target: str = "") -> dict:
        State, Nonce, Verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        T = _Utc()
        self.Db.Execute("DELETE FROM oauth_states WHERE expires_at < ?", (_Iso(T - timedelta(hours=1)),))
        self.Db.Execute("INSERT INTO oauth_states (state_digest, provider, nonce, verifier, purpose, account_id, target, created_at, "
                        "expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (_Digest("oauth", State), Provider, Nonce, Verifier, Purpose, AccountId, Target, _Iso(T),
                         _Iso(T + timedelta(minutes=10))))
        return {"state": State, "nonce": Nonce, "verifier": Verifier}

    def UseOAuthState(self, Provider: str, State: str) -> dict | None:
        Row = self.Db.One("SELECT * FROM oauth_states WHERE state_digest = ? AND provider = ?", (_Digest("oauth", State or ""), Provider))
        if Row is None or Row["used_at"] or Row["expires_at"] < _Iso(_Utc()):
            return None
        if not self.Db.Execute("UPDATE oauth_states SET used_at = ? WHERE state_digest = ? AND used_at IS NULL", (Now(), Row["state_digest"])):
            return None
        return Row

    # ── sessions ─────────────────────────────────────────────────────────
    def CreateSession(self, AccountId: str, Method: str, UserAgent: str | None = None) -> dict:
        """A new session (a new random id: a pre-login id is never reused). Returns {token, csrf, expires_at}."""
        Token, Csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        T = _Utc()
        self.Db.Execute(
            "INSERT INTO customer_sessions (id, session_digest, csrf, account_id, auth_method, auth_at, created_at, last_seen_at, "
            "expires_at, absolute_expires_at, user_agent) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("ses_" + secrets.token_hex(8), _Digest("session", Token), Csrf, AccountId, Method, _Iso(T), _Iso(T), _Iso(T),
             _Iso(T + timedelta(days=SessionIdleDays)), _Iso(T + timedelta(days=SessionAbsoluteDays)), (UserAgent or "")[:200]))
        self.Accounts.RecordSignIn(AccountId, Method)
        return {"token": Token, "csrf": Csrf, "max_age": SessionIdleDays * 86400}

    def ResolveSession(self, Token: str | None) -> dict | None:
        """The live session for this cookie value, or None. Slides the idle expiry (written at most every 30 minutes)."""
        if not Token or len(Token) > 200:
            return None
        Row = self.Db.One("SELECT s.*, a.display_name, a.status, a.removed_at FROM customer_sessions s "
                          "JOIN accounts a ON a.account_id = s.account_id WHERE s.session_digest = ?", (_Digest("session", Token),))
        if Row is None or Row["revoked_at"] or Row["removed_at"] or Row["status"] != "active":
            return None
        T = _Utc()
        if Row["expires_at"] < _Iso(T) or Row["absolute_expires_at"] < _Iso(T):
            return None
        if Row["last_seen_at"] < _Iso(T - timedelta(minutes=SessionTouchMinutes)):
            Idle = min(T + timedelta(days=SessionIdleDays), datetime.fromisoformat(Row["absolute_expires_at"]))
            self.Db.Execute("UPDATE customer_sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?", (_Iso(T), _Iso(Idle), Row["id"]))
        return Row

    def PrincipalFor(self, Session: dict) -> Principal:
        return Principal(AccountId=Session["account_id"], DisplayName=Session["display_name"] or "", Issuer=self.Accounts.Issuer,
                         Attributes={"session_id": Session["id"], "auth_method": Session["auth_method"]})

    def IsRecent(self, Session: dict) -> bool:
        return Session["auth_at"] >= _Iso(_Utc() - timedelta(minutes=RecentAuthMinutes))

    def Reauthenticated(self, SessionId: str, Method: str) -> None:
        self.Db.Execute("UPDATE customer_sessions SET auth_at = ?, auth_method = ? WHERE id = ?", (Now(), Method, SessionId))

    def CsrfOk(self, Session: dict, Supplied: str | None) -> bool:
        return bool(Supplied) and hmac.compare_digest(Supplied.encode(), Session["csrf"].encode())

    def Revoke(self, Token: str | None, Reason: str = "logout") -> bool:
        if not Token:
            return False
        return self.Db.Execute("UPDATE customer_sessions SET revoked_at = ?, revoked_reason = ? WHERE session_digest = ? "
                               "AND revoked_at IS NULL", (Now(), Reason, _Digest("session", Token))) > 0

    def RevokeAll(self, AccountId: str, ExceptId: str | None = None, Reason: str = "revoked") -> int:
        return self.Db.Execute("UPDATE customer_sessions SET revoked_at = ?, revoked_reason = ? WHERE account_id = ? AND revoked_at IS NULL "
                               "AND id != ?", (Now(), Reason, AccountId, ExceptId or ""))

    def Rotate(self, Session: dict, Reason: str = "rotated") -> dict:
        """After a sensitive change: a new session id for this browser, every other session of the account revoked."""
        self.RevokeAll(Session["account_id"], Reason=Reason)
        New = self.CreateSession(Session["account_id"], Session["auth_method"])
        self.Db.Execute("UPDATE customer_sessions SET auth_at = ? WHERE session_digest = ?", (Session["auth_at"], _Digest("session", New["token"])))
        return New

    def ListSessions(self, AccountId: str, CurrentId: str | None = None) -> list[dict]:
        Rows = self.Db.All("SELECT id, auth_method, created_at, last_seen_at, user_agent FROM customer_sessions WHERE account_id = ? "
                           "AND revoked_at IS NULL AND expires_at > ? AND absolute_expires_at > ? ORDER BY last_seen_at DESC",
                           (AccountId, Now(), Now()))
        return [{**R, "current": R["id"] == CurrentId} for R in Rows]

    def RevokeById(self, AccountId: str, SessionId: str) -> bool:
        return self.Db.Execute("UPDATE customer_sessions SET revoked_at = ?, revoked_reason = 'revoked' WHERE id = ? AND account_id = ? "
                               "AND revoked_at IS NULL", (Now(), SessionId, AccountId)) > 0

    # ── legacy six-letter code ───────────────────────────────────────────
    def LegacyAccountForToken(self, Token: str) -> dict | None:
        """The account a still-active six-letter code opens (no side effects), else None."""
        from p3.accounts.local import HashToken
        return self.Db.One("SELECT a.* FROM access_tokens t JOIN accounts a ON a.account_id = t.account_id WHERE t.token_hash = ? "
                           "AND t.active = 1 AND a.status = 'active' AND a.removed_at IS NULL", (HashToken(Token),))

    def Summary(self, AccountId: str, SessionId: str | None = None) -> dict:
        """What the Account page shows: connected methods, whether a password is set, whether the email is verified."""
        Ids = self.Identities(AccountId)
        A = self._Account(AccountId)
        Pw = next((I for I in Ids if I["provider"] == "password"), None)
        return {"email": A["email"] or "", "email_verified": self.EmailVerified(AccountId),
                "has_password": Pw is not None, "password_email": Pw["email"] if Pw else "",
                "methods": [{"id": I["id"], "provider": I["provider"], "email": I["email"] or "", "created_at": I["created_at"],
                             "can_disconnect": self.HasMethod(AccountId, Excluding=I["id"])} for I in Ids],
                "legacy_code_active": bool(self.Db.One("SELECT 1 AS x FROM access_tokens WHERE account_id = ? AND active = 1", (AccountId,))),
                "sessions": self.ListSessions(AccountId, SessionId)}


    # ── migration and monitoring (operator CLI, Admin health) ─────────────
    def LegacyReport(self) -> dict:
        """Where the six-letter-code migration stands: a read-only picture (no writes)."""
        One = lambda Sql: (self.Db.One(Sql) or {}).get("n", 0)
        Dup = self.Db.All("SELECT lower(email) AS email, COUNT(*) AS n FROM accounts WHERE email IS NOT NULL AND email != '' AND removed_at IS NULL "
                          "GROUP BY lower(email) HAVING COUNT(*) > 1")
        return {
            "accounts_live": One("SELECT COUNT(*) AS n FROM accounts WHERE removed_at IS NULL"),
            "with_login": One("SELECT COUNT(DISTINCT account_id) AS n FROM login_identities WHERE account_id IN (SELECT account_id FROM accounts WHERE removed_at IS NULL)"),
            "legacy_unclaimed": One("SELECT COUNT(DISTINCT t.account_id) AS n FROM access_tokens t JOIN accounts a ON a.account_id = t.account_id "
                                    "WHERE t.active = 1 AND a.removed_at IS NULL AND a.account_id NOT IN (SELECT account_id FROM login_identities)"),
            "legacy_unclaimed_without_email": One("SELECT COUNT(DISTINCT t.account_id) AS n FROM access_tokens t JOIN accounts a ON a.account_id = t.account_id "
                                                  "WHERE t.active = 1 AND a.removed_at IS NULL AND (a.email IS NULL OR a.email = '') "
                                                  "AND a.account_id NOT IN (SELECT account_id FROM login_identities)"),
            "claimed": One("SELECT COUNT(*) AS n FROM accounts WHERE claimed_at IS NOT NULL"),
            "duplicate_emails": [{"email": D["email"], "accounts": D["n"]} for D in Dup],
        }

    def RetireUnclaimedCodes(self, DryRun: bool = True) -> int:
        """End of the transition window: deactivate every six-letter code that was never exchanged for a login. The accounts,
        credits and history stay; their owners recover with "Forgot password" by email. Returns how many codes it affects."""
        Where = ("FROM access_tokens WHERE active = 1 AND account_id NOT IN (SELECT account_id FROM login_identities)")
        N = (self.Db.One("SELECT COUNT(*) AS n " + Where) or {}).get("n", 0)
        if not DryRun:
            self.Db.Execute("UPDATE access_tokens SET active = 0 WHERE active = 1 AND account_id NOT IN (SELECT account_id FROM login_identities)")
        return N

    def Stats(self) -> dict:
        """Counters for monitoring: sign-ins in the last 24 h by method, live sessions, links sent, and the migration picture."""
        Since = _Iso(_Utc() - timedelta(hours=24))
        Rows = self.Db.All("SELECT detail, COUNT(*) AS n FROM account_events WHERE kind = 'sign_in' AND created_at >= ? GROUP BY detail", (Since,))
        Sent = self.Db.All("SELECT purpose, COUNT(*) AS n FROM auth_tokens WHERE created_at >= ? GROUP BY purpose", (Since,))
        Live = (self.Db.One("SELECT COUNT(*) AS n FROM customer_sessions WHERE revoked_at IS NULL AND expires_at > ? AND absolute_expires_at > ?", (Now(), Now())) or {}).get("n", 0)
        return {"sign_ins_24h": {R["detail"]: R["n"] for R in Rows}, "links_issued_24h": {R["purpose"]: R["n"] for R in Sent},
                "live_sessions": Live, "migration": self.LegacyReport()}
