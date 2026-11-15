"""Normalization helpers for global movie-search ignore terms."""

from collections.abc import Iterable
import re
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


def remove_ignored_terms(text: str, ignored_terms: Iterable[str]) -> str:
    """Remove ignored words or phrases from user search text."""
    tokens = []
    token_chars = []
    token_start = None
    token_end = None

    def finish_token():
        if token_chars:
            tokens.append(("".join(token_chars), token_start, token_end))
            token_chars.clear()

    for index, character in enumerate(text):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        for normalized_character in normalized:
            if normalized_character.isalnum():
                if token_start is None:
                    token_start = index
                token_end = index + 1
                token_chars.append(normalized_character)
            else:
                finish_token()
                token_start = None
                token_end = None
    finish_token()

    term_tokens = {
        tuple(term.split())
        for term in ignored_terms
        if term.strip()
    }
    spans = []
    index = 0
    while index < len(tokens):
        match_length = 0
        for term in term_tokens:
            length = len(term)
            if (
                length > match_length
                and tuple(token[0] for token in tokens[index:index + length])
                == term
            ):
                match_length = length

        if match_length:
            start = tokens[index][1]
            end = tokens[index + match_length - 1][2]
            while start > 0 and not text[start - 1].isalnum():
                start -= 1
            while end < len(text) and not text[end].isalnum():
                end += 1
            spans.append((start, end))
            index += match_length
        else:
            index += 1

    if not spans:
        return text.strip()

    parts = []
    cursor = 0
    for start, end in spans:
        if start < cursor:
            cursor = max(cursor, end)
            continue
        parts.append(text[cursor:start])
        parts.append(" ")
        cursor = end
    parts.append(text[cursor:])
    return re.sub(r"\s+", " ", "".join(parts)).strip()
