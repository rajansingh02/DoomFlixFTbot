"""Normalization helpers for global movie-search ignore terms."""

from collections.abc import Iterable
import unicodedata


def normalize_search_text(value: str) -> str:
    """Case-fold text and treat punctuation, symbols, and emoji as separators."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    parts = []
    pending_separator = False

    for character in normalized:
        if character.isalnum():
            if pending_separator and parts:
                parts.append(" ")
            parts.append(character)
            pending_separator = False
        else:
            pending_separator = True

    return "".join(parts)


def contains_ignored_term(
    text: str,
    ignored_terms: Iterable[str],
) -> bool:
    """Match normalized ignored terms anywhere in the post text."""
    normalized_text = normalize_search_text(text)
    if not normalized_text:
        return False

    return any(
        term and term in normalized_text
        for term in ignored_terms
    )
