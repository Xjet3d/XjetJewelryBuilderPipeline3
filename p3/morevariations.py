"""“More variations” (R-50 follow-up, owner decision of 2026-10-25): a refinement that asks for more options instead of a
change — “make more variations”, “show me others”, “עוד אפשרויות” — becomes four variations of the selected design from
one fixed instruction, so the model never answers it with several pieces in one picture (customer session R-1040).

A plain word list in English and Hebrew decides; no AI call. A request matches only when every word in it is on the list
and one of them asks for more or other ones: a request that names anything else (“more gold”, “other colors”, “thinner”)
is an ordinary refinement."""

import re

# Words that ask for more or other ones (one of them must be there)
Variety = {
    "more", "other", "others", "different", "new", "another", "else", "similar", "alternative", "alternatives",
    "variation", "variations", "options",
    "עוד", "יותר", "נוסף", "נוספת", "נוספים", "נוספות", "אחר", "אחרת", "אחרים", "אחרות", "שונה", "שונים", "שונות",
    "חדש", "חדשה", "חדשים", "חדשות", "דומה", "דומים", "דומות", "וריאציה", "וריאציות", "ואריאציה", "ואריאציות",
    "אפשרויות", "אלטרנטיבות",
}
# What they may ask for
Things = {
    "variation", "variations", "option", "options", "design", "designs", "version", "versions", "idea", "ideas",
    "choice", "choices", "alternative", "alternatives", "one", "ones", "something",
    "וריאציה", "וריאציות", "אפשרות", "אפשרויות", "עיצוב", "עיצובים", "הצעה", "הצעות", "גרסה", "גרסאות", "גירסה",
    "גירסאות", "רעיון", "רעיונות", "דגם", "דגמים", "משהו", "אחד", "אחת", "כאלה", "כאלו",
}
# The words around them that change nothing (“please”, “can you show me”, “of this ring”, “תראה לי”, “בבקשה”)
Filler = {
    "please", "pls", "plz", "can", "could", "would", "will", "you", "u", "i", "i'd", "id", "i'm", "me", "us", "we",
    "want", "wanna", "like", "love", "need", "see", "show", "give", "get", "make", "create", "generate", "try", "have",
    "let", "let's", "lets", "some", "a", "an", "the", "few", "couple", "bunch", "several", "of", "for", "to", "this",
    "that", "these", "those", "it", "same", "kind", "sort", "ring", "charm", "piece", "ok", "okay", "now", "just",
    "maybe", "also", "too", "as", "well", "again", "thanks", "thank", "thx", "hi", "hey", "and", "yes",
    "1", "2", "3", "4", "two", "three", "four",
    "בבקשה", "תן", "תני", "תנו", "לי", "לנו", "תראה", "תראי", "תראו", "הראה", "הראי", "תציג", "תציגי", "אפשר", "לקבל",
    "לראות", "אני", "רוצה", "רוצים", "אשמח", "תעשה", "תעשי", "תעשו", "תייצר", "תייצרי", "תכין", "תכיני", "צור", "צרי",
    "תיצור", "תיצרי", "תוכל", "תוכלי", "יכול", "יכולה", "את", "של", "זה", "הזה", "הזאת", "הזו", "זאת", "כמו", "קצת",
    "כמה", "גם", "עכשיו", "רק", "טבעת", "תליון", "צ'ארם", "תודה", "היי", "אוקיי", "כן", "שתיים", "שלוש", "ארבע",
}
Words = Variety | Things | Filler
Prefixes = "והלבשמכ"                              # Hebrew one-letter prefixes: “ועוד”, “לעוד”, “האפשרויות”
MaxWords = 12

Instructions = {
    "ring": "Make one new variation of this ring in the same style: keep its overall character and metal, "
            "and redesign its details.",
    "charm": "Make one new variation of this charm in the same style: keep its overall character, its metal and its "
             "attachment eyelet, and redesign its details.",
}


def _Words(Text: str) -> list[str]:
    Text = re.sub(r"[֑-ׇ]", "", (Text or "").lower())          # Hebrew vowel points and cantillation
    Text = Text.replace("’", "'").replace("׳", "'")
    return re.findall(r"[a-z0-9א-ת']+", Text)


def _Known(Word: str) -> str | None:
    """The list's own form of the word (a Hebrew word may carry a one-letter prefix), or None."""
    if Word in Words:
        return Word
    if len(Word) > 2 and Word[0] in Prefixes and Word[1:] in Words:
        return Word[1:]
    return None


def IsMoreVariations(Text: str) -> bool:
    Found = [_Known(W.strip("'")) for W in _Words(Text)]
    return 0 < len(Found) <= MaxWords and None not in Found and any(W in Variety for W in Found)


def Instruction(Product: str) -> str:
    return Instructions.get(Product, Instructions["ring"])


def WasUsed(EffectivePrompt: str | None) -> bool:
    """For the Admin: a batch whose prompt carries the fixed instruction (the customer asked for more variations)."""
    return any(T in (EffectivePrompt or "") for T in Instructions.values())
