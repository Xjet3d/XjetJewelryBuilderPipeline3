"""Terms acceptance (2026-10-29): the legal texts customers accept, the sign-ups waiting to be accepted and the record of
every acceptance. In accounts.db beside the accounts, so an account and its acceptance are written in one transaction.

* legal_documents        every version of the Terms of Service and the Privacy Policy as the site served them: the text
                         a reader gets, its SHA-256 and a version number per document. A text not served before becomes
                         the document's next version the first time it is asked for (Current): versions are never typed in.
* consents               one row per acceptance: the account, when (UTC), how the account came to be (password, google,
                         apple, email = the older sign-up with a six-letter code, admin = an account XJet made) and the
                         version and hash of each document. No address, password, token or IP is kept with it.
* pending_registrations  a sign-up that is not an account yet: an email and password (or the older email sign-up) waiting
                         for its emailed link, or a Google / Apple sign-in waiting for the Terms. No credits, gone after
                         24 hours, never listed with a whole address.
* accounts.terms_required an account that accepts at its next sign-in before anything else: one XJet made (the Admin, the
                         operator's CLI), or a registration begun before acceptance existed and finished only now.

An account made before all this has no acceptance ("legacy"): it is never asked, and nothing is recorded for it after the
fact. Customers' wording is business-approved by the owner (28-29 Oct 2026) and not yet reviewed by legal counsel.
"""

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from p3.db import Now

Documents = ("terms", "privacy")
PendingTtlHours = 24
MaxPendingPerEmail = 5             # a few sign-ups per address at once (each with its own link); the oldest go first

Schema = """
CREATE TABLE IF NOT EXISTS legal_documents (
    doc             TEXT NOT NULL CHECK (doc IN ('terms', 'privacy')),
    version         INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    text            TEXT NOT NULL,           -- what a reader of the page gets: its words, link targets and pictures
    first_served_at TEXT NOT NULL,
    PRIMARY KEY (doc, version),
    UNIQUE (doc, sha256)
);

CREATE TABLE IF NOT EXISTS consents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT NOT NULL,
    accepted_at     TEXT NOT NULL,           -- UTC
    method          TEXT NOT NULL,           -- password | google | apple | email | admin
    terms_version   INTEGER NOT NULL,
    terms_sha256    TEXT NOT NULL,
    privacy_version INTEGER NOT NULL,
    privacy_sha256  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS consents_account ON consents(account_id, accepted_at);

CREATE TABLE IF NOT EXISTS pending_registrations (
    id               TEXT PRIMARY KEY,
    method           TEXT NOT NULL CHECK (method IN ('password', 'google', 'apple', 'email')),
    secret_digest    TEXT NOT NULL UNIQUE,   -- sha256 of the emailed link's secret, or of the browser's cookie
    email            TEXT,                   -- lower case
    display_name     TEXT NOT NULL DEFAULT '',
    password_hash    TEXT,                   -- argon2id, for an email-and-password sign-up
    provider_subject TEXT,                   -- the Google / Apple user id
    email_verified   INTEGER NOT NULL DEFAULT 0,   -- the provider vouches for the address
    terms_version    INTEGER,                -- what an email sign-up accepted on its form (checked again when its link is used)
    terms_sha256     TEXT,
    privacy_version  INTEGER,
    privacy_sha256   TEXT,
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    used_at          TEXT,                   -- became its account: only the digest, the times and that account's id stay,
    account_id       TEXT                    -- until it expires (an old link then says what became of the account)
);
CREATE INDEX IF NOT EXISTS pending_registrations_email ON pending_registrations(email);
"""


class TermsChanged(Exception):
    """The versions a customer accepted are not the ones the site serves now: the Terms or the Privacy Policy changed
    during the sign-up, so the current ones are accepted again."""

    def __init__(self, Current: dict):
        super().__init__("terms_changed")
        self.Current = Current


class ConsentError(Exception):
    """A refusal the customer may be told about (Code is stable, Message is customer-safe)."""

    def __init__(self, Code: str, Message: str):
        super().__init__(Message)
        self.Code, self.Message = Code, Message


LinkMessages = {"used": "This link was already used. If that was you, just sign in.",
                "expired": "This sign-up has expired. Please sign up again.",
                "invalid": "This link is not valid. Please sign up again."}


def _Utc() -> datetime:
    return datetime.now(timezone.utc)


def _Iso(Dt: datetime) -> str:
    return Dt.isoformat(timespec="milliseconds")


def _Digest(Secret: str) -> str:
    return hashlib.sha256(("pending:" + (Secret or "").strip()).encode("utf-8")).hexdigest()


def Sha256(Text: str) -> str:
    return hashlib.sha256((Text or "").encode("utf-8")).hexdigest()


def MaskEmail(Email: str | None) -> str:
    """dana@example.com → d***@e***.com: all the Admin sees of an address nobody has confirmed."""
    Local, _, Domain = (Email or "").partition("@")
    if not Domain:
        return "***"
    Name, Dot, Tld = Domain.rpartition(".")
    return f"{Local[:1]}***@{(Name or Tld)[:1]}***{Dot}{Tld if Name else ''}"


def AcceptedOf(Row: dict) -> dict | None:
    """The versions a pending email sign-up accepted on its form, or None."""
    if Row.get("terms_version") is None or Row.get("privacy_version") is None:
        return None
    return {"terms": {"version": Row["terms_version"], "sha256": Row["terms_sha256"]},
            "privacy": {"version": Row["privacy_version"], "sha256": Row["privacy_sha256"]}}


class ConsentStore:
    def __init__(self, Accounts):
        self.Accounts = Accounts                   # the LocalAccountProvider that owns the Db
        self.Db = Accounts.Db
        self.Texts = None                          # the app's () -> {"terms": text, "privacy": text}, as served now
        with self.Db.Connect() as Conn:
            Conn.executescript(Schema)
        with self.Db.Transaction() as Conn:
            if "terms_required" not in {R[1] for R in Conn.execute("PRAGMA table_info(accounts)")}:
                Conn.execute("ALTER TABLE accounts ADD COLUMN terms_required INTEGER NOT NULL DEFAULT 0")
            Cols = {R[1] for R in Conn.execute("PRAGMA table_info(pending_registrations)")}
            for Col in ("used_at", "account_id"):
                if Col not in Cols:
                    Conn.execute(f"ALTER TABLE pending_registrations ADD COLUMN {Col} TEXT")

    # ── the legal texts ──────────────────────────────────────────────────
    def Current(self) -> dict:
        """{"terms": {"version", "sha256"}, "privacy": {…}}: the versions the site serves now. A text not seen before is
        archived as its document's next version (the same text again is the version it already was)."""
        if self.Texts is None:
            raise RuntimeError("The legal texts are not connected (ConsentStore.Texts)")
        Texts, Out = self.Texts(), {}
        for Doc in Documents:
            Text = Texts.get(Doc) or ""
            Sha = Sha256(Text)
            Row = self.Db.One("SELECT version FROM legal_documents WHERE doc = ? AND sha256 = ?", (Doc, Sha))
            if Row is None:
                with self.Db.Transaction() as Conn:
                    Hit = Conn.execute("SELECT version FROM legal_documents WHERE doc = ? AND sha256 = ?", (Doc, Sha)).fetchone()
                    if Hit is None:
                        Next = (Conn.execute("SELECT MAX(version) FROM legal_documents WHERE doc = ?", (Doc,)).fetchone()[0] or 0) + 1
                        Conn.execute("INSERT INTO legal_documents (doc, version, sha256, text, first_served_at) VALUES (?,?,?,?,?)",
                                     (Doc, Next, Sha, Text, Now()))
                        Row = {"version": Next}
                    else:
                        Row = {"version": Hit["version"]}
            Out[Doc] = {"version": int(Row["version"]), "sha256": Sha}
        return Out

    def Check(self, Given) -> dict:
        """The versions a customer accepted ({"terms": {"version", "sha256"}, "privacy": {…}}, as the page sent them) are
        the ones served now: returns them; TermsChanged (carrying the current ones) otherwise."""
        Cur = self.Current()
        for Doc in Documents:
            G = (Given or {}).get(Doc) if isinstance(Given, dict) else None
            if not isinstance(G, dict) or G.get("version") != Cur[Doc]["version"] or G.get("sha256") != Cur[Doc]["sha256"]:
                raise TermsChanged(Cur)
        return Cur

    def Archive(self) -> list[dict]:
        """Every archived version, newest first (the Admin): without the texts."""
        return self.Db.All("SELECT doc, version, sha256, first_served_at, length(text) AS characters FROM legal_documents "
                           "ORDER BY doc, version DESC")

    # ── acceptances ──────────────────────────────────────────────────────
    @staticmethod
    def _Record(Conn, AccountId: str, Method: str, V: dict) -> None:
        T = Now()
        Conn.execute("INSERT INTO consents (account_id, accepted_at, method, terms_version, terms_sha256, privacy_version, "
                     "privacy_sha256) VALUES (?,?,?,?,?,?,?)",
                     (AccountId, T, Method, V["terms"]["version"], V["terms"]["sha256"], V["privacy"]["version"], V["privacy"]["sha256"]))
        Conn.execute("INSERT INTO account_events (account_id, kind, detail, created_at) VALUES (?, 'terms_accepted', ?, ?)",
                     (AccountId, f"Terms v{V['terms']['version']} · Privacy v{V['privacy']['version']} · {Method}", T))

    def Required(self, AccountId: str) -> bool:
        Row = self.Db.One("SELECT terms_required FROM accounts WHERE account_id = ?", (AccountId,))
        return bool(Row and Row["terms_required"])

    def Status(self, AccountId: str) -> dict:
        """accepted (with the latest acceptance) | pending (accepts at its sign-in) | legacy (made before acceptance)."""
        Row = self.Db.One("SELECT accepted_at, method, terms_version, privacy_version FROM consents WHERE account_id = ? "
                          "ORDER BY accepted_at DESC, id DESC LIMIT 1", (AccountId,))
        if Row:
            return {"status": "accepted", **Row}
        return {"status": "pending" if self.Required(AccountId) else "legacy"}

    def AcceptForAccount(self, AccountId: str, Given) -> str:
        """An account that accepts at its sign-in (terms_required) accepts now: the record and the end of the wait in one
        transaction. Any other account records nothing — an accepted one has its record, a legacy one is never recorded
        after the fact. Returns 'accepted' | 'not_required'; TermsChanged when the versions are not the current ones."""
        V = self.Check(Given)
        with self.Db.Transaction() as Conn:
            A = Conn.execute("SELECT source FROM accounts WHERE account_id = ? AND terms_required = 1", (AccountId,)).fetchone()
            if A is None:
                return "not_required"
            Login = Conn.execute("SELECT provider FROM login_identities WHERE account_id = ? ORDER BY created_at LIMIT 1",
                                 (AccountId,)).fetchone()
            Method = "admin" if A["source"] == "admin" else "email" if A["source"] == "self" else (Login["provider"] if Login else "password")
            self._Record(Conn, AccountId, Method, V)
            Conn.execute("UPDATE accounts SET terms_required = 0 WHERE account_id = ?", (AccountId,))
        return "accepted"

    def HoldIfUnfinished(self, AccountId: str) -> None:
        """A registration begun before acceptance existed and finished only now (its email confirmed, or a provider vouching
        for its address) never accepted anything: it accepts at its first sign-in, like an account XJet made. Call before
        the account is marked verified."""
        self.Db.Execute("UPDATE accounts SET terms_required = 1 WHERE account_id = ? AND source IN ('self', 'auth') AND "
                        "verified_at IS NULL AND last_sign_in_at IS NULL AND removed_at IS NULL AND "
                        "account_id NOT IN (SELECT account_id FROM consents)", (AccountId,))

    # ── pending registrations ────────────────────────────────────────────
    def NewPending(self, Method: str, Email: str | None, Name: str = "", PasswordHash: str | None = None,
                   Subject: str | None = None, EmailVerified: bool = False, Accepted: dict | None = None,
                   Replace: bool = False) -> str:
        """A sign-up that is not an account yet; returns its secret (only the digest is kept): the emailed link's, or the
        browser cookie's for Google / Apple. Accepted: the versions an email sign-up accepted on its form. Replace: the
        older email sign-up's rule (P2) — only the latest link of an address works (PendingName keeps the name given
        first). An email-and-password sign-up keeps each attempt's own link instead, so a later one never swaps in its
        password."""
        Secret, T = secrets.token_urlsafe(32), _Utc()
        Email = (Email or "").strip().lower() or None
        A = Accepted or {}
        with self.Db.Transaction() as Conn:
            Conn.execute("DELETE FROM pending_registrations WHERE expires_at < ?", (_Iso(T),))
            if Subject:                            # one waiting sign-in per Google / Apple user: the latest
                Conn.execute("DELETE FROM pending_registrations WHERE method = ? AND provider_subject = ?", (Method, Subject))
            if Replace and Email:
                Conn.execute("DELETE FROM pending_registrations WHERE method = ? AND email = ? AND used_at IS NULL", (Method, Email))
            if Email:
                Older = [R[0] for R in Conn.execute("SELECT id FROM pending_registrations WHERE email = ? ORDER BY created_at DESC",
                                                    (Email,))]
                for Id in Older[MaxPendingPerEmail - 1:]:
                    Conn.execute("DELETE FROM pending_registrations WHERE id = ?", (Id,))
            Conn.execute("INSERT INTO pending_registrations (id, method, secret_digest, email, display_name, password_hash, "
                         "provider_subject, email_verified, terms_version, terms_sha256, privacy_version, privacy_sha256, "
                         "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("preg_" + secrets.token_hex(8), Method, _Digest(Secret), Email, (Name or "").strip()[:100], PasswordHash,
                          Subject, 1 if EmailVerified else 0, (A.get("terms") or {}).get("version"), (A.get("terms") or {}).get("sha256"),
                          (A.get("privacy") or {}).get("version"), (A.get("privacy") or {}).get("sha256"), _Iso(T),
                          _Iso(T + timedelta(hours=PendingTtlHours))))
        return Secret

    def Pending(self, Secret: str | None) -> dict | None:
        """The live pending registration for this secret (not used, not expired), or None."""
        if not Secret or len(Secret) > 200:
            return None
        Row = self.Db.One("SELECT * FROM pending_registrations WHERE secret_digest = ?", (_Digest(Secret),))
        return Row if Row and not Row["used_at"] and Row["expires_at"] >= _Iso(_Utc()) else None

    def PendingState(self, Secret: str | None) -> str:
        """'valid' | 'used' | 'expired' | 'invalid' — for the page a link opens (no detail beyond that)."""
        if not Secret or len(Secret) > 200:
            return "invalid"
        Row = self.Db.One("SELECT expires_at, used_at FROM pending_registrations WHERE secret_digest = ?", (_Digest(Secret),))
        if Row is None:
            return "invalid"
        if Row["used_at"]:
            return "used"
        return "valid" if Row["expires_at"] >= _Iso(_Utc()) else "expired"

    def PendingName(self, Method: str, Email: str | None) -> str:
        """The name an address's waiting sign-up of this kind was given first ("" if none)."""
        Row = self.Db.One("SELECT display_name FROM pending_registrations WHERE method = ? AND email = ? AND used_at IS NULL "
                          "AND expires_at >= ? ORDER BY created_at LIMIT 1", (Method, (Email or "").strip().lower(), _Iso(_Utc())))
        return Row["display_name"] if Row else ""

    def Waiting(self, Method: str, Email: str | None) -> dict | None:
        """An address's latest live sign-up of this kind (not used, not expired), or None."""
        return self.Db.One("SELECT * FROM pending_registrations WHERE method = ? AND email = ? AND used_at IS NULL AND "
                           "expires_at >= ? ORDER BY created_at DESC LIMIT 1", (Method, (Email or "").strip().lower(), _Iso(_Utc())))

    def Renew(self, Method: str, Email: str | None) -> dict | None:
        """"Send the confirmation link again" for a sign-up waiting for its link: a new link (a new 24 hours) for the
        address's latest one, with its name, password and acceptance; earlier links work until theirs end. Returns
        {"secret", "name"}, or None when nothing waits."""
        P = self.Waiting(Method, Email)
        if P is None:
            return None
        Secret = self.NewPending(Method, P["email"], P["display_name"], PasswordHash=P["password_hash"], Accepted=AcceptedOf(P))
        return {"secret": Secret, "name": P["display_name"]}

    def UsedBy(self, Secret: str | None) -> str | None:
        """The account a used sign-up's link became (while its record lasts), or None."""
        if not Secret or len(Secret) > 200:
            return None
        Row = self.Db.One("SELECT account_id FROM pending_registrations WHERE secret_digest = ? AND used_at IS NOT NULL",
                          (_Digest(Secret),))
        return Row["account_id"] if Row else None

    def DropPending(self, Secret: str | None) -> bool:
        return bool(Secret) and self.Db.Execute("DELETE FROM pending_registrations WHERE secret_digest = ?", (_Digest(Secret),)) > 0

    def ListPending(self) -> list[dict]:
        """The sign-ups waiting (the Admin, read only): how, when, until when, and a masked address — never the address,
        a password hash, a secret or a provider id."""
        self.Db.Execute("DELETE FROM pending_registrations WHERE expires_at < ?", (_Iso(_Utc()),))
        return [{"id": R["id"], "method": R["method"], "email": MaskEmail(R["email"]), "created_at": R["created_at"],
                 "expires_at": R["expires_at"]}
                for R in self.Db.All("SELECT id, method, email, created_at, expires_at FROM pending_registrations "
                                     "WHERE used_at IS NULL ORDER BY created_at DESC")]

    def CreateAccount(self, Secret: str, Versions: dict) -> dict:
        """The account a pending registration becomes, written with its login, its welcome credits (the Admin's starting
        credits of this moment) and its acceptance (Versions, checked by the caller) in one transaction: all of it, or none.
        The address's other sign-ups go with it. Returns {"account_id", "method", "name", "email", "token" (the older
        email sign-up's six-letter code, else None)}. ConsentError: link_invalid / link_expired / link_used /
        email_in_use / already_linked."""
        from p3.accounts.identity import IsRelayEmail
        from p3.accounts.local import HashToken, NewAccountId
        State = self.PendingState(Secret)
        P = self.Pending(Secret) if State == "valid" else None
        if P is None:
            raise ConsentError("link_" + State, LinkMessages.get(State, LinkMessages["invalid"]))
        Method, Email = P["method"], P["email"]
        Relay = IsRelayEmail(Email)
        AccountEmail = None if Relay else Email
        Verified = Method in ("password", "email") or bool(P["email_verified"])
        Name = P["display_name"] or (Email.split("@")[0] if Email and not Relay and Method in ("google", "apple") else "")
        Token = self.Accounts.GenerateToken() if Method == "email" else None
        AccountId, T, Taken = NewAccountId(), Now(), False
        try:
            with self.Db.Transaction() as Conn:
                # Used once: nothing of the person stays on it (the account has it now), only the digest until it expires
                if not Conn.execute("UPDATE pending_registrations SET used_at = ?, email = NULL, display_name = '', "
                                    "password_hash = NULL, provider_subject = NULL WHERE id = ? AND used_at IS NULL",
                                    (T, P["id"])).rowcount:
                    raise ConsentError("link_used", LinkMessages["used"])
                # The address got an account another way meanwhile: never a second one (or a second welcome credit)
                Taken = bool(AccountEmail and Conn.execute("SELECT 1 FROM accounts WHERE lower(email) = ?", (AccountEmail,)).fetchone())
                if not Taken:
                    Conn.execute("UPDATE pending_registrations SET account_id = ? WHERE id = ?", (AccountId, P["id"]))
                if not Taken:
                    Conn.execute("INSERT INTO accounts (account_id, display_name, email, created_at, source, max_generations, "
                                 "verified_at, activated_at, delivery_token) VALUES (?,?,?,?,?,?,?,?,?)",
                                 (AccountId, Name, AccountEmail, T, "self" if Method == "email" else "auth",
                                  int(self.Accounts.StartingCredits()), T if Verified and AccountEmail else None, T, Token))
                    if Method == "email":
                        Conn.execute("INSERT INTO access_tokens (token_hash, token_hint, account_id, label, active, created_at) "
                                     "VALUES (?,?,?, 'self-registration', 1, ?)", (HashToken(Token), Token[:3], AccountId, T))
                    else:
                        Provider = "password" if Method == "password" else Method
                        Conn.execute("INSERT INTO login_identities (id, account_id, provider, subject, email, email_verified, "
                                     "display_name, created_at, last_used_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                     ("lid_" + secrets.token_hex(8), AccountId, Provider,
                                      AccountId if Provider == "password" else P["provider_subject"], Email, 1 if Verified else 0,
                                      "" if Provider == "password" else Name, T, T))
                        if Provider == "password":
                            Conn.execute("INSERT INTO password_credentials (account_id, hash, updated_at) VALUES (?,?,?)",
                                         (AccountId, P["password_hash"], T))
                    Conn.execute("INSERT INTO account_events (account_id, kind, detail, created_at) VALUES (?, 'account_created', ?, ?)",
                                 (AccountId, Method, T))
                    self._Record(Conn, AccountId, Method, Versions)
                    if Email:
                        Conn.execute("DELETE FROM pending_registrations WHERE email = ?", (Email,))
        except sqlite3.IntegrityError as E:       # the Google / Apple user or the password's address was taken meanwhile
            raise ConsentError("already_linked" if Method in ("google", "apple") else "email_in_use",
                               "That sign-in already belongs to an XJet Atelier account. Please sign in instead.") from E
        if Taken:
            raise ConsentError("email_in_use", "This email already has an XJet Atelier account. Please sign in instead.")
        return {"account_id": AccountId, "method": Method, "name": Name, "email": AccountEmail or "", "token": Token}
