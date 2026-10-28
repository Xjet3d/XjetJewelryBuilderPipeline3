"""Atelier's content policy of 28 Oct (P1–P7) in the prompt-check instructions: config/prompts/ring_anyllm_system.txt and
charm_anyllm_system.txt (the Charm seed), each activated as a new version on a site's any-llm page. Faces are allowed and
public figures still stopped (P1); one metal per piece, uniform gold plating counting as one (P2); a charm may join several
motifs into one connected, rigid piece with one eyelet (P3); protected logos and characters need the customer's rights
(P4); a stopped request says why and how to change it, with one of the check's codes (P5). The model and its settings
stay in each site's active versions, unchanged (P7). The customer page tells a refusal from a technical failure (P5)."""

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


def test_p1_faces_are_allowed_and_public_figures_are_still_stopped():
    for Text in Texts.values():
        assert "Do not reject a request because it shows or describes a human face, and do not ask for proof of permission." in Text
        assert "the faces of family, friends, loved ones or other people whose permission they have" in Text
        assert "named or clearly identifiable real public figure" in Text


def test_p2_one_metal_per_piece_and_uniform_gold_plating_is_one_metal():
    for Product, Text in Texts.items():
        assert "Reject explicitly requested mixed metals or two-tone designs" in Text, Product
        assert f"Uniform gold plating over the whole {Product} is one metal and is allowed." in Text, Product
        assert "14K gold over stainless steel (stainless steel with uniform 14K gold plating)" in Text, Product
        assert "vermeil" not in Text.lower(), Product


def test_p3_a_charm_joins_several_motifs_into_one_rigid_piece_with_one_eyelet():
    Charm = Texts["charm"]
    assert "A charm may combine several motifs or elements" in Charm
    assert "when they form one connected, rigid metal body with a single eyelet at the top" in Charm
    assert "- Separate, floating, dangling or moving parts, hinges, or articulated components." in Charm
    assert "- Several separate charms, or more than one eyelet or attachment loop." in Charm
    assert "Multiple charms or additional attachment loops" not in Charm


def test_p4_protected_content_needs_the_customers_rights():
    for Text in Texts.values():
        assert "A reproduction of protected content that the customer does not say they own or are licensed to use" in Text
        assert "Logos, crests, names and characters that the customer says they own or are licensed to use" in Text
        assert "are not a reason to reject" not in Text                     # the brand rule of 9 Oct is replaced


def test_p5_a_stopped_request_says_why_and_how_to_change_it_and_the_page_shows_it_as_a_rule():
    for Text in Texts.values():
        assert "what cannot be made and why, then how to change the request so that it can be made" in Text
    # The prompt check's refusal has its own block, never the red error line of a technical problem
    assert "if (e.code === 'prompt_rejected') this.composeRefusal = e.message;" in App
    Block = Index[Index.index('<div x-show="composeRefusal"'):]
    Block = Block[:Block.index("</div>")]
    assert 'role="alert"' in Block and "We can’t make this design" in Block and 'x-text="composeRefusal"' in Block
    # A refusal under the image service's safety rules says what to change; a technical failure says it was not the words
    assert "under its safety rules" in Images.CustomerFailureText["content_policy"] and "under its safety rules" in Images.PolicyRetryText
    assert "because of a technical problem, not your description. Your credit was returned." in App
    assert "We can’t make pictures of real people" not in App                # P1: faces are allowed
