"""Atelier: switch Pipeline 3 from development to production mode (P3_ENV=production), 3 Nov 2026.
For Dov, on the Atelier server, as dov — through p3_production_mode.sh, which passes the checkout:

  check          read-only: the .env changes production needs (no secret is printed) and a dry run of the new settings
  enroll-owner   enrols the owner's Google identity for the Admin, after the owner tried to sign in once (README)
  apply          backup (.env + both databases), the new .env, restart, checks; rolls back by itself on any failure
  rollback DIR   puts the .env from that backup back and restarts

What production changes (p3/settings.py ValidateProduction, P3 code 3 Nov or later):
  P3_ENV=production; the AI mode follows P3_PROVIDER=fal (the developer switch no longer applies) with FAL_KEY in .env
  (copied from the key saved in the Admin, which production ignores); P3_ADMIN_KEY and P3_SIGNING_SECRET at least 24
  random characters (new ones when shorter); the Admin answers only through the SSH tunnel (P3_ADMIN_HOST=localhost,
  P3_ADMIN_BASE_URL=http://localhost:8340); the Admin opens only with an enrolled identity (P3_ADMIN_IDENTITY_ENFORCED):
  the shared key opens nothing through the web — it stays the break-glass key for `python -m p3.cli` on this server.
Nothing else changes: data, accounts, designs, orders, settings and the customer site stay as they are."""
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

Checkout = Path(sys.argv[1]).resolve()
Cmd = sys.argv[2] if len(sys.argv) > 2 else "check"
EnvPath = Checkout / ".env"
Data = Checkout / "var"
Service = os.environ.get("P3_SERVICE", "xjet-jewelry-b2c3")
Port = int(os.environ.get("P3_TUNNEL_PORT", "8340"))
Base = os.environ.get("P3_BASE", "/JewelryB2C3")
PublicHost = os.environ.get("P3_PUBLIC_HOST", "xjetatelier.xjet3d.com")
Owner = os.environ.get("P3_OWNER_ADMIN_EMAIL", "yakir.tubul@xjet3d.com").lower()
OwnerName = os.environ.get("P3_OWNER_ADMIN_NAME", "Yakir")
MinSecret = 24


def Say(*A):
    print(*A, flush=True)


def ReadEnv() -> tuple[list[str], dict]:
    Lines = EnvPath.read_text(encoding="utf-8").splitlines()
    Values = {}
    for L in Lines:
        M = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", L)
        if M and not L.lstrip().startswith("#"):
            Values[M.group(1)] = M.group(2).strip().strip('"').strip("'")
    return Lines, Values


def WriteEnv(Lines: list[str], Changes: dict) -> str:
    Out, Done = [], set()
    for L in Lines:
        M = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", L)
        if M and not L.lstrip().startswith("#") and M.group(1) in Changes:
            K = M.group(1)
            if K in Done:
                continue                                    # a duplicate line of a changed key goes
            Out.append(f"{K}={Changes[K]}")
            Done.add(K)
        else:
            Out.append(L)
    New = [K for K in Changes if K not in Done]
    if New:
        Out += ["", f"# production mode ({datetime.now(timezone.utc):%Y-%m-%d}, p3_production_mode.py)"] + [f"{K}={Changes[K]}" for K in New]
    return "\n".join(Out) + "\n"


def OwnerIdentity() -> tuple[dict | None, int]:
    """The owner's enrolled Google identity and how many Admin sessions it opened (a proof that the sign-in works)."""
    C = sqlite3.connect(f"file:{Data / 'pipeline3.db'}?mode=ro", uri=True)
    C.row_factory = sqlite3.Row
    try:
        R = C.execute("SELECT id, name, email, enabled FROM admin_identities WHERE provider = 'google' AND lower(email) = ? "
                      "ORDER BY enabled DESC, added_at DESC LIMIT 1", (Owner,)).fetchone()
        if R is None:
            return None, 0
        N = C.execute("SELECT COUNT(*) FROM admin_sessions WHERE admin_identity_id = ?", (R["id"],)).fetchone()[0]
        return dict(R), N
    finally:
        C.close()


def Plan() -> tuple[dict, list[str], list[str]]:
    """(changes, notes, problems): the .env lines production needs. Secrets never reach the output."""
    _, V = ReadEnv()
    Changes, Notes, Problems = {}, [], []
    Code = (Checkout / "p3" / "settings.py").read_text(encoding="utf-8")
    if "TunnelHosts" not in Code:
        Problems.append("the P3 code is older than 3 Nov: production does not accept the tunnel Admin yet (wait for the release)")
    Want = {"P3_ENV": "production", "P3_PROVIDER": "fal", "P3_ADMIN_HOST": "localhost",
            "P3_ADMIN_BASE_URL": f"http://localhost:{Port}", "P3_ADMIN_IDENTITY_ENFORCED": "true"}
    for K, W in Want.items():
        if V.get(K, "") != W:
            Changes[K] = W
            Notes.append(f"{K}: {V.get(K, '(unset)')} -> {W}")
    Saved = (Data / "fal_key.secret").read_text(encoding="utf-8").strip() if (Data / "fal_key.secret").exists() else ""
    if Saved and V.get("FAL_KEY", "") != Saved:
        Changes["FAL_KEY"] = Saved
        Notes.append(f"FAL_KEY: the key saved in the Admin (…{Saved[-4:]}) is written to .env (production ignores the Admin's copy)")
    elif not Saved and not V.get("FAL_KEY"):
        Problems.append("no fal key: neither FAL_KEY in .env nor a key saved in the Admin")
    Admin = V.get("P3_ADMIN_KEY", "")
    if (Data / "admin_key.secret").exists():
        Notes.append("note: an Admin key saved in the Admin exists; production ignores it and uses P3_ADMIN_KEY from .env")
    if len(Admin) < MinSecret:
        Admin = secrets.token_urlsafe(24)
        Changes["P3_ADMIN_KEY"] = Admin
        Notes.append(f"P3_ADMIN_KEY: {len(V.get('P3_ADMIN_KEY', ''))} characters -> a new random key of {len(Admin)} (the break-glass key; the web no longer accepts it)")
    Sign = V.get("P3_SIGNING_SECRET", "")
    if len(Sign) < MinSecret or Sign == Admin:
        Changes["P3_SIGNING_SECRET"] = secrets.token_urlsafe(24)
        Notes.append("P3_SIGNING_SECRET: a new random secret of 32 characters (signed download links made before the switch stop working)")
    if V.get("P3_AUTH_MODE", "legacy") not in ("modern", "dual"):
        Problems.append(f"P3_AUTH_MODE is {V.get('P3_AUTH_MODE', 'legacy')!r}: production needs modern (set by B1 on 2 Nov) — check before switching")
    Who, Sessions = OwnerIdentity()
    if Who is None or not Who["enabled"]:
        Problems.append(f"the owner's Google identity ({Owner}) is not enrolled for the Admin: README steps 1–3 first")
    elif Sessions < 1:
        Problems.append(f"the owner's Google identity is enrolled but has never opened the Admin: README step 3 first (proof that Google sign-in works through the tunnel)")
    else:
        Notes.append(f"owner's Google identity: enrolled and enabled, {Sessions} Admin session(s) opened")
    # a dry run of the new settings in a fresh interpreter (the running service is not touched)
    Env = {**os.environ, **{K: V2 for K, V2 in V.items()}, **Changes}
    R = subprocess.run([sys.executable, "-I", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); from p3.settings import LoadSettings; S = LoadSettings(); "
                        "from p3.modes import ResolveStartupMode; "
                        "print(S.Env, S.Provider, S.AdminHost, S.AdminBaseUrl, S.BasePath, S.DataDir, S.AdminIdentityEnforced, ResolveStartupMode(S)[0])",
                        str(Checkout)], env=Env, cwd=str(Checkout), capture_output=True, text=True, timeout=120)
    if R.returncode != 0:
        Problems.append("the dry run refuses the new settings:\n" + "\n".join("      " + L for L in (R.stderr or R.stdout).strip().splitlines()[-12:]))
    else:
        Notes.append("dry run: " + R.stdout.strip() + "  (env, provider, admin host, admin base URL, base path, data, identity enforced, AI mode)")
    return Changes, Notes, Problems


def Get(Path_: str, Host: str) -> tuple[int, str]:
    Req = urllib.request.Request(f"http://127.0.0.1:{Port}{Base}{Path_}", headers={"Host": Host, "X-Forwarded-Host": Host, "User-Agent": "p3-production-mode"})
    try:
        with urllib.request.urlopen(Req, timeout=20) as Resp:
            return Resp.status, Resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as E:
        return E.code, E.read().decode("utf-8", "replace")
    except Exception as E:  # noqa: BLE001
        return 0, str(E)


def Restart() -> None:
    subprocess.run(["systemctl", "--user", "restart", Service], check=True)
    for _ in range(60):
        time.sleep(1)
        if Get("/api/health", PublicHost)[0] == 200:
            return
    raise RuntimeError("the service did not answer /api/health within 60 s")


def Verify(Production: bool) -> list[str]:
    Bad = []
    C, Home = Get("/", PublicHost)
    if C != 200 or "window.__p3" not in Home:
        Bad.append(f"the homepage answers {C}")
    Env = re.search(r'"env": "([a-z]+)"', Home)
    if not Env or Env.group(1) != ("production" if Production else "development"):
        Bad.append(f"the page says env {Env.group(1) if Env else '?'}")
    if '"dev_tools": false' not in Home:
        Bad.append("developer tools are on in the page")
    for P in ("/dev", "/showcase", "/api/dev/mode", "/docs"):
        if Get(P, PublicHost)[0] != 404:
            Bad.append(f"{P} answers")
    if Production:
        if Get("/api/admin/health", PublicHost)[0] != 404:
            Bad.append("the Admin API answers on the public host")
        C, Body = Get("/api/admin/health", f"localhost:{Port}")
        if C != 403 or "admin_identity_required" not in Body:
            Bad.append(f"through the tunnel the Admin API should ask for an administrator sign-in (got {C})")
        C, Body = Get("/api/admin/auth/config", f"localhost:{Port}")
        if C != 200 or '"identity_enforced": true' not in Body.replace('":true', '": true'):
            Bad.append(f"the Admin sign-in config does not say identity_enforced (got {C})")
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://{PublicHost}{Base}/", headers={"User-Agent": "Mozilla/5.0 (p3-production-mode)"}), timeout=30) as R:
            if R.status != 200:
                Bad.append(f"https://{PublicHost}{Base}/ answers {R.status}")
    except Exception as E:  # noqa: BLE001
        Bad.append(f"https://{PublicHost}{Base}/: {E}")
    return Bad


def Backup() -> Path:
    D = Path.home() / "p3-backups" / f"production-mode-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    D.mkdir(parents=True)
    os.chmod(D, 0o700)
    shutil.copy2(EnvPath, D / ".env")
    os.chmod(D / ".env", 0o600)
    for Name in ("pipeline3.db", "accounts.db"):
        Src = sqlite3.connect(f"file:{Data / Name}?mode=ro", uri=True)
        Dst = sqlite3.connect(D / Name)
        with Dst:
            Src.backup(Dst)
        Ok = Dst.execute("PRAGMA integrity_check").fetchone()[0]
        Src.close(); Dst.close()
        if Ok != "ok":
            raise RuntimeError(f"the backup of {Name} does not verify: {Ok}")
        os.chmod(D / Name, 0o600)
    return D


def Rollback(D: Path) -> None:
    Say(f"== rollback: the .env from {D}")
    Tmp = EnvPath.with_suffix(".rollback")
    shutil.copy2(D / ".env", Tmp)
    os.replace(Tmp, EnvPath)
    Restart()
    Bad = Verify(Production=False)
    Say("   restored; checks " + ("passed" if not Bad else "FAILED: " + "; ".join(Bad)))


def Main() -> None:
    if Cmd == "check":
        Changes, Notes, Problems = Plan()
        Say("== production mode — what would change in .env (no secret is shown)")
        for N in Notes:
            Say("   " + N)
        Say("== problems: " + ("none — ready to apply" if not Problems else ""))
        for P in Problems:
            Say("   - " + P)
        sys.exit(1 if Problems else 0)

    elif Cmd == "enroll-owner":
        Lines, V = ReadEnv()
        R = subprocess.run([sys.executable, "-m", "p3.cli", "admin-candidates"], cwd=str(Checkout), capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(Checkout)})
        Rows = [L.split("\t") for L in R.stdout.splitlines() if L.count("\t") >= 6]
        Mine = [X for X in Rows if X[0] == "google" and X[2].strip().lower() == Owner and X[3] == "verified"]
        if not Mine:
            Say(f"No refused Google sign-in from {Owner} with a verified email yet: the owner signs in once first (README step 1).")
            Say("Candidates seen:\n" + (R.stdout.strip() or "   (none)"))
            sys.exit(1)
        Subject = Mine[0][1]
        R = subprocess.run([sys.executable, "-m", "p3.cli", "admin-enroll", "--name", OwnerName, "--provider", "google",
                            "--subject", Subject, "--email", Owner, "--admin-key", V.get("P3_ADMIN_KEY", "")],
                           cwd=str(Checkout), capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(Checkout)})
        Say((R.stdout or R.stderr).strip())
        sys.exit(R.returncode)

    elif Cmd == "apply":
        Changes, Notes, Problems = Plan()
        if Problems:
            Say("Refused — nothing changed:")
            for P in Problems:
                Say("   - " + P)
            sys.exit(1)
        if not Changes:
            Say("Nothing to change: production mode is already set.")
            sys.exit(0)
        D = Backup()
        Say(f"== backup: {D} (.env + both databases, verified)")
        Lines, _ = ReadEnv()
        Tmp = EnvPath.with_suffix(".production")
        Tmp.write_text(WriteEnv(Lines, Changes), encoding="utf-8")
        os.chmod(Tmp, 0o600)
        os.replace(Tmp, EnvPath)
        Say("== .env written: " + ", ".join(sorted(Changes)))
        try:
            Restart()
            Bad = Verify(Production=True)
        except Exception as E:  # noqa: BLE001
            Bad = [str(E)]
        if Bad:
            Say("== checks FAILED: " + "; ".join(Bad))
            Rollback(D)
            sys.exit(1)
        Say("== production mode is on; checks passed (env production, developer tools off, Admin only through the tunnel and "
            "only with an administrator sign-in, the public site answers)")
        Say(f"   rollback if ever needed: bash p3_production_mode.sh rollback {D}")
        sys.exit(0)

    elif Cmd == "rollback":
        Rollback(Path(sys.argv[3]).resolve())

    else:
        Say(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    Main()
