from __future__ import annotations

import itertools
import re
import unicodedata

_TOKEN = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]")
_PUNCTUATION = " \t\r\n,，.。;；:：!?！？、()（）[]【】{}<>《》'\"`"


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    # Split lexical camelCase, but preserve alphanumeric business codes such as 00D4.
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = text.replace("_", " ").replace("-", " ")
    text = text.lower().strip(_PUNCTUATION)
    return " ".join(text.split())


def tokenize(value: object) -> frozenset[str]:
    pieces = _TOKEN.findall(normalize_text(value))
    tokens = set(pieces)
    tokens.update(
        item[:-1]
        for item in pieces
        if len(item) > 3 and item.endswith("s") and not item.endswith("ss")
    )
    tokens.update(
        item[:-3]
        for item in pieces
        if len(item) >= 7 and item.endswith("ing") and len(item[:-3]) >= 4
    )
    if "num" in tokens:
        tokens.add("number")
    if "number" in tokens:
        tokens.add("num")
    han = [item for item in pieces if len(item) == 1 and "\u3400" <= item <= "\u9fff"]
    tokens.update(left + right for left, right in itertools.pairwise(han))
    return frozenset(tokens)


__all__ = ["normalize_text", "tokenize"]
