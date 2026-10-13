"""Checks for text the AI wrote while tailoring.

  style_problems(text)              AI writing tells (ported from style-check.sh)
  new_numbers(new, original)        numbers in new that the original doesn't have
  new_terms(new, original)          tool or product names in new that the original doesn't have
"""
import re

# The same patterns as style-check.sh. Each one is a common sign of AI-written text.
STYLE_PATTERN = re.compile(
    r"—| – |it.?s not about|it is not about|not just .{0,30}(it.?s|but)|not only .{0,30}but also"
    r"|it.?s not .{0,30}[, ]+it.?s|leverag|seamless|robust|delve|tapestry|realm of|in the world of"
    r"|navigate the landscape|game.?changer|supercharg|unlock|elevate|in today.?s|when it comes to"
    r"|it.?s worth noting|needless to say|at the end of the day|furthermore|moreover|additionally,"
    r"|a testament|speaks volumes|paints a picture|cutting.?edge|state.of.the.art",
    re.IGNORECASE,
)


def style_problems(text):
    """The AI-writing tells found in text, as a list like ['leverag', '—']."""
    return [match.group(0) for match in STYLE_PATTERN.finditer(text)]


def numbers_in(text):
    """Numbers in text, normalized: '10,000+' -> '10000', '87%' -> '87', '4.5' -> '4.5'."""
    found = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    return {n.replace(",", "") for n in found}


def new_numbers(new, original):
    """Numbers in the rewritten text that the original doesn't contain."""
    return sorted(numbers_in(new) - numbers_in(original))


def term_candidates(text):
    """Words that look like names of tools, products or companies: anything with a
    capital letter after the first word, or containing a digit or one of # + . /
    For example: Python, Azure, CI/CD, C#, Node.js, GCP."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9.+#/-]*[A-Za-z0-9+#]|[A-Za-z]", text)
    candidates = set()
    for position, word in enumerate(words):
        looks_like_a_name = any(c.isupper() for c in word[1:]) or (word[0].isupper() and position > 0)
        has_symbol = any(c.isdigit() or c in "#+./" for c in word)
        if looks_like_a_name or has_symbol:
            candidates.add(word)
    return candidates


def new_terms(new, original):
    """Tool or product names in the rewritten text that the original doesn't mention.
    A rewritten bullet may change wording, but not add technologies."""
    original_lower = original.lower()
    return sorted(term for term in term_candidates(new) if term.lower() not in original_lower)