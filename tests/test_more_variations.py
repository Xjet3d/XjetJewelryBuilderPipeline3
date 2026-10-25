"""“More variations” (p3/morevariations.py, R-50 follow-up): a plain English and Hebrew word list decides, no AI call. A
request matches only when every word in it is on the list and one asks for more or other ones."""

import pytest

from p3 import morevariations as MoreVariations


@pytest.mark.parametrize("Text", [
    "Make more variations", "make more variations please", "More options", "Show me others", "something else",
    "another one", "Can you make some different versions?", "more like this", "4 more options!", "I'd like to see other designs",
    "more", "Variations", "new ones", "make it different",
    "עוד וריאציות", "תראה לי עוד אפשרויות", "אפשרויות נוספות בבקשה", "משהו אחר", "ועוד", "האפשרויות האחרות",
    "תן לי עוד 4 הצעות", "אשמח לעוד עיצובים",
])
def test_a_request_for_more_of_them_matches(Text):
    assert MoreVariations.IsMoreVariations(Text)


@pytest.mark.parametrize("Text", [
    "Make the band thinner", "more gold", "other colors", "a different style", "more variations with a thinner band",
    "again", "try again", "show me", "", "   ", "Add a leaf motif and make more options", "make more options " * 5,
    "תעשה את זה דק יותר", "עוד זהב", "יותר עדין", "עוד אפשרויות עם יהלום",
])
def test_anything_else_is_an_ordinary_refinement(Text):
    assert not MoreVariations.IsMoreVariations(Text)


def test_the_fixed_instruction_names_the_product_and_is_recognised_in_a_prompt():
    Ring, Charm = MoreVariations.Instruction("ring"), MoreVariations.Instruction("charm")
    assert "ring" in Ring and "charm" not in Ring and "charm" in Charm and "eyelet" in Charm
    assert MoreVariations.Instruction("unknown") == Ring
    assert MoreVariations.WasUsed(Ring + "\n\nUse the reference image…") and not MoreVariations.WasUsed("Thinner band") and not MoreVariations.WasUsed(None)
