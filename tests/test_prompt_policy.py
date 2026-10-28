"""Atelier's content policy of 28 Oct (P1–P5, with the owner's clarifications of the same day) in the prompt-check
instructions: config/prompts/ring_anyllm_system.txt and charm_anyllm_system.txt (the Charm seed), each activated as a new
version on a site's any-llm page. Faces are allowed, and a public figure's likeness only with the customer's statement of
commercial permission (P1); one metal per piece, uniform gold plating and any finish of the one metal allowed (P2); a
charm may join several motifs into one connected, rigid piece with one eyelet (P3); logos, brands, team names, crests,
characters and fan designs are not stopped by the check — the image service's own refusals are respected (P4); a stopped
request says why and how to change it, with one of the check's codes (P5). The model and its settings stay in each
site's active versions, unchanged (P7). The customer page tells a refusal from a technical failure (P5)."""

import re
from pathlib import Path

from p3 import images as Images
from p3 import promptcheck as PromptCheck

Root = Path(__file__).resolve().parents[1]
Texts = {P: (Root / "config" / "prompts" / f"{P}_anyllm_system.txt").read_text(encoding="utf-8") for P in ("ring", "charm")}
App = (Root / "web" / "app.js").read_text(encoding="utf-8")
Index = (Root / "web" / "index.html").read_text(encoding="utf-8")


def Codes(Text: str) -> list[str]:
    Section = Text[Text.index("Set rejection_category"):Text.index("APPLICATION CONTEXT")]
    return re.findall(r'^- "([a-z_]+)":', Section, re.M)


def test_both_instructions_ask_for_the_decision_and_list_exactly_the_codes_the_check_knows():
    for Product, Text in Texts.items():
        assert Text.startswith(f"You validate requests for an AI {Product}-design application.")
        assert all(F in Text for F in PromptCheck.Fields + ("reason_for_rejection", "rejection_category", "refined_prompt")), Product
        assert Codes(Text) == list(PromptCheck.Categories), Product


def test_p1_faces_are_allowed_and_a_public_figure_needs_the_customers_statement_of_permission():
    for Text in Texts.values():
        assert "Do not reject a request because it shows or describes a human face, and do not ask for proof of permission." in Text
        assert "the faces of family, friends, loved ones or other people whose permission they have" in Text
        assert ("named or clearly identifiable real public figure, such as a politician, celebrity, athlete, royal or influencer, "
                "unless the customer states that they have appropriate commercial permission to use that person's likeness. "
                "Do not ask for proof: accept the customer's statement.") in Text


def test_p2_one_metal_per_piece_while_plating_and_finishes_are_allowed():
    for Product, Text in Texts.items():
        assert "Reject explicitly requested mixed metals or two-tone designs: two metals or two metal colors" in Text, Product
        assert f"Uniform gold plating over the whole {Product} is one metal and is allowed." in Text, Product
        assert ("Finishes of the one metal, such as polished, matte, brushed, hammered, blackened or oxidized, are not a second "
                "metal and are allowed.") in Text, Product
        assert "14K gold over stainless steel (stainless steel with uniform 14K gold plating)" in Text, Product
        assert "vermeil" not in Text.lower(), Product


def test_p3_a_charm_joins_several_motifs_into_one_rigid_piece_with_one_eyelet():
    Charm = Texts["charm"]
    assert "A charm may combine several motifs or elements" in Charm
    assert "when they form one connected, rigid metal body with a single eyelet at the top" in Charm
    assert "- Separate, floating, dangling or moving parts, hinges, or articulated components." in Charm
    assert "- Several separate charms, or more than one eyelet or attachment loop." in Charm
    assert "Multiple charms or additional attachment loops" not in Charm


def test_p4_logos_brands_teams_and_characters_are_not_stopped_by_the_check():
    for Text in Texts.values():
        assert ("Logos, brand names, team names, club crests, characters from films, cartoons, anime, games or comics, and "
                "other fan designs are not a reason to reject") in Text
        assert "protected content" not in Text and "licensed to use" not in Text


def test_p5_a_stopped_request_says_why_and_how_to_change_it_and_the_page_shows_it_as_a_rule():
    for Product, Text in Texts.items():
        Rejections = Text[Text.index("\nREJECTIONS\n"):Text.index("Set rejection_category")]
        assert "- First, what cannot be made and why." in Rejections, Product
        assert "- Then, how to change the request so that it can be made" in Rejections, Product
        assert "Always write both sentences, even when the change seems obvious." in Rejections, Product
        assert f"one of the metals for the whole {Product}; say that the customer picks the metal in Customize" in Rejections, Product
        # No example sentence to copy: the check repeated the quoted examples word for word, without the reason (28 Oct)
        assert '"' not in Rejections, Product
    assert ("- Openwork, lattice or decorative holes: suggest a solid charm with the same pattern engraved, recessed or in "
            "relief on its surface instead of cut through.") in Texts["charm"]
    # The prompt check's refusal has its own block, never the red error line of a technical problem
    assert "if (e.code === 'prompt_rejected') this.composeRefusal = e.message;" in App
    Block = Index[Index.index('<div x-show="composeRefusal"'):]
    Block = Block[:Block.index("</div>")]
    assert 'role="alert"' in Block and "We can’t make this design" in Block and 'x-text="composeRefusal"' in Block
    # A refusal under the image service's safety rules says what to change; a technical failure says it was not the words
    assert "under its safety rules" in Images.CustomerFailureText["content_policy"] and "under its safety rules" in Images.PolicyRetryText
    assert "because of a technical problem, not your description. Your credit was returned." in App
    assert "We can’t make pictures of real people" not in App                # P1: faces are allowed
