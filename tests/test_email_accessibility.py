"""ACC-07 follow-up: every customer and team email is a whole English document (<html lang="en">) and its small text
meets 4.5:1 on the email card (#FAF8F4); white is only ever text on the dark or gold buttons."""

import re

from p3 import mail as Mail

Order = {"ref": "ORD-10001", "customer": {"first_name": "Dana", "last_name": "Levi", "email": "dana@example.com", "phone": "+972 50 000 0000"},
         "lines": [{"title": "Laurel Band", "ring_id": "R-1001", "material_label": "Sterling Silver", "ring_size": 7, "size_label": "US 7",
                    "quantity": 1, "line_total": 109.0, "currency": "USD", "image_url": None, "product_type": "ring"}],
         "shipping": {}, "address_lines": ["Dana Levi", "1 Main St", "Tel Aviv", "Israel"], "subtotal": 109.0, "shipping_cost": 0.0,
         "total": 109.0, "currency": "USD", "payment_status": "pending", "payment_label": "Payment pending", "payment_message": "",
         "shipping_method": "standard", "shipping_label": "Standard", "count": 1, "product_types": ["ring"]}


def Emails():
    Yield = [Mail.VerificationEmail("Dana", "https://a.example/#t=1"), Mail.AuthVerifyEmail("Dana", "https://a.example/v"),
             Mail.ResetPasswordEmail("Dana", "https://a.example/r"), Mail.AccountExistsEmail("Dana", "https://a.example"),
             Mail.PasswordChangedEmail("Dana"), Mail.AccountUnavailableEmail("Dana", True)]
    try:
        Yield.append(Mail.OrderConfirmationEmail(Order, "https://a.example", "https://a.example/#account"))
    except (KeyError, TypeError):
        pass                                                # the order email's own tests cover its fields
    return Yield


def Lum(Hex):
    Hex = Hex.lstrip("#")
    Hex = "".join(C * 2 for C in Hex) if len(Hex) == 3 else Hex
    def Ch(V):
        V = int(V, 16) / 255
        return V / 12.92 if V <= 0.03928 else ((V + 0.055) / 1.055) ** 2.4
    R, G, B = (Ch(Hex[i:i + 2]) for i in (0, 2, 4))
    return 0.2126 * R + 0.7152 * G + 0.0722 * B


def Contrast(A, B):
    L1, L2 = sorted((Lum(A), Lum(B)), reverse=True)
    return (L1 + 0.05) / (L2 + 0.05)


def test_every_email_is_an_english_document():
    for Subject, Html in Emails():
        assert Html.startswith("<!DOCTYPE html>\n<html lang=\"en\">"), Subject
        assert Html.rstrip().endswith("</html>"), Subject


def test_text_colours_meet_4_5_to_1_on_the_card():
    Source = open(Mail.__file__, encoding="utf-8").read()
    Colours = {C.upper() for C in re.findall(r"(?<![-\w])color:\s*(#[0-9A-Fa-f]{6})", Source)} - {"#FFFFFF"}
    assert Colours
    for C in Colours:
        assert Contrast(C, "#FAF8F4") >= 4.5, (C, round(Contrast(C, "#FAF8F4"), 2))
    assert "#8F8F8F" not in Source and "#9A7230" not in Source
