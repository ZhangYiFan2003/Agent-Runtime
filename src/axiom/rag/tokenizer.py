from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable

import jieba

from axiom.rag.models import CodeChunk

jieba.setLogLevel(logging.ERROR)

CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
LETTER_NUMBER_BOUNDARY_RE = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return unicodedata.normalize("NFKC", value).casefold()


def split_identifier(value: str) -> list[str]:
    normalized = normalize_text(value)
    if not normalized:
        return []

    tokens: list[str] = [normalized]
    for part in re.split(r"[_\-\s./\\:]+", value):
        if not part:
            continue
        normalized_part = normalize_text(part)
        tokens.append(normalized_part)
        camel_parts = CAMEL_BOUNDARY_RE.split(part)
        if len(camel_parts) > 1 and len(camel_parts[0]) == 1 and camel_parts[1][1:].islower():
            camel_parts = [camel_parts[0] + camel_parts[1], *camel_parts[2:]]
        for camel_part in camel_parts:
            for number_part in LETTER_NUMBER_BOUNDARY_RE.split(camel_part):
                normalized_number_part = normalize_text(number_part)
                if normalized_number_part:
                    tokens.append(normalized_number_part)
    return _dedupe(tokens)


def tokenize_code_text(value: str | None) -> list[str]:
    normalized = normalize_text(value)
    if not normalized:
        return []

    tokens: list[str] = []
    for raw in TOKEN_RE.findall(value or ""):
        tokens.extend(split_identifier(raw))
    if CHINESE_RE.search(value or ""):
        tokens.extend(normalize_text(token) for token in jieba.cut(value or "") if token.strip())
    return _dedupe(token for token in tokens if token.strip())


def tokenize_query(value: str | None) -> list[str]:
    return tokenize_code_text(value)


def compact_identifier(value: str | None) -> str:
    """Normalize qualified, snake_case, and camelCase identifiers for matching."""
    return "".join(character for character in normalize_text(value) if character.isalnum())


def identifier_forms(value: str | None) -> set[str]:
    """Return whole-query and component forms without repository-specific aliases."""
    if not value:
        return set()
    forms = {compact_identifier(value)}
    for part in re.split(r"[\s./\\:]+", value):
        if part:
            forms.add(compact_identifier(part))
    return {form for form in forms if form}


def build_lexical_text(chunk: CodeChunk) -> str:
    fields = [
        chunk.content,
        chunk.symbol_name or "",
        chunk.qualified_name or "",
        chunk.file_path,
        chunk.chunk_type,
        chunk.parent_symbol or "",
        chunk.language,
    ]
    tokens: list[str] = []
    for field in fields:
        tokens.extend(tokenize_code_text(field))
    return " ".join(_dedupe(tokens))


def fts_match_query(tokens: Iterable[str]) -> str:
    quoted = [_quote_fts_token(token) for token in tokens if token.strip()]
    return " AND ".join(quoted)


def fts_any_query(tokens: Iterable[str]) -> str:
    quoted = [_quote_fts_token(token) for token in tokens if token.strip()]
    return " OR ".join(quoted)


def _quote_fts_token(token: str) -> str:
    safe = token.replace('"', '""')
    return f'"{safe}"'


def _dedupe(tokens: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result
