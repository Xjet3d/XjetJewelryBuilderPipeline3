"""Test records (the owner's request of 2026-10-13, §6; Fulfillment's decision I-31).

A synthetic order is one placed for a test: its customer's address is at a domain reserved for tests and documentation
(RFC 2606 / RFC 6761 — example.com, shop.test, synthetic@example.invalid …), which no real customer can have. Such an
order is marked Test on the Admin pages and in what Fulfillment receives (`order.test`), is kept out of the
dashboard's figures, and sends its customer no email; a real customer's emails are untouched. Fulfillment applies the
same rule to a snapshot that does not carry the verdict."""

ReservedDomains = ("example.com", "example.net", "example.org", "example", "test", "invalid", "localhost")
ReservedSuffixes = (".example", ".test", ".invalid", ".localhost")


def ReservedEmail(Email) -> bool:
    """True when the address is at a reserved test domain."""
    if not isinstance(Email, str) or "@" not in Email:
        return False
    Domain = Email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    return Domain in ReservedDomains or Domain.endswith(ReservedSuffixes)


def IsTestCustomer(Customer) -> bool:
    """A record whose customer is a test customer (a dict with an `email`, as orders and quote requests keep it)."""
    return ReservedEmail((Customer or {}).get("email") if isinstance(Customer, dict) else None)
