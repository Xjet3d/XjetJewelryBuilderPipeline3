"""The repository's ignore rules (2026-10-18): no secret, key, certificate or backup copy can be added by accident. The
repository is public, and a server checkout must never hold a copy one `git add` away from it
(docs/ATELIER-DRIFT-2026-10-16.md, deploy/README.md)."""
from pathlib import Path

Root = Path(__file__).resolve().parents[1]


def test_secret_key_certificate_and_backup_copies_are_ignored():
    Rules = {L.strip() for L in (Root / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for Rule in (".env", ".env.*", "*.bak", "*.bak-*", "*.secret", "backups/", "*.priv", "*.key", "*.pem", "*.p12", "*.pfx"):
        assert Rule in Rules, Rule
    assert "!.env.example" in Rules and "!.env.*.example" in Rules          # the documented templates stay in Git
