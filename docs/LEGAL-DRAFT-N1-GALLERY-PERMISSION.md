# DRAFT — per-design Gallery and resale permission (N1)

**Status: DRAFT — pending legal counsel's written approval. Text only: nothing in the code asks for, records or
checks this permission on this branch (`b5-legal-drafts`, never pushed). A consent mechanism needs the owner's
approval first.** Source: Counsel Book, Appendix A-7 (owner decision AI-03: keep the live T11 licence and add a
separate per-design customer authorisation for Gallery display and resale). The Terms draft (web/index.html, Terms
section 7 "Your designs") already says the permission is asked per design.

## Where it would be shown

On a design the customer created (for example on Customize or in My Designs), unticked by default. Not offered when
the design shows an identifiable person or carries personal details (a name, initials, a date), unless a separate
likeness permission is recorded.

## Wording

> ☐ **Share this design in the Inspiration Gallery**
>
> I allow XJet Ltd. to show this design in its public Inspiration Gallery and on its marketing channels, and to make
> and sell jewelry based on it to other customers, as described in the Terms of Service (Your designs).
>
> - I will not be paid royalties [or: I will receive [N] credits when another customer orders it].
> - My name, my uploaded photos and my personal details will not be shown.
> - I confirm that this design does not show a person who has not agreed to it, and that I have the rights to
>   anything in it that belongs to someone else.
> - I can withdraw this permission at any time from my account or by writing to [support email]. XJet then removes
>   the design from the gallery within [7 days] and stops offering it; orders already placed will still be made.

## What would be recorded (once approved and built)

Per design: time (UTC), account, design ID, wording version (as the Terms consent records do,
p3/accounts/consent.py). Withdrawal: time (UTC) and how it was made.

## Open points for counsel / XJet

- Royalties: none, or [N] credits when another customer orders the design (owner).
- Removal period after withdrawal: [7 days].
- [support email]: the site's configured support address (atelier@xjet3d.com today).
- The Admin's existing consent note for customer designs in the gallery (Admin → Gallery) would be replaced by this
  record; designs already in the gallery need a decision (ask each customer, or keep under T11).
