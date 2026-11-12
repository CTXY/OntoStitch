from __future__ import annotations

import itertools
import re
import unicodedata

_TOKEN = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]")
_PUNCTUATION = " \t\r\n,，.。;；:：!?！？、()（）[]【】{}<>《》'\"`"


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip(_PUNCTUATION)
    return " ".join(text.split())


def tokenize(value: object) -> frozenset[str]:
    pieces = _TOKEN.findall(normalize_text(value))
    tokens = set(pieces)
    # Preserve the original word and add a conservative singular form so plural Question mentions
    # can recall singular ontology names before semantic selection.
    tokens.update(
        item[:-1]
        for item in pieces
        if len(item) > 3 and item.endswith("s") and not item.endswith("ss")
    )
    han = [item for item in pieces if len(item) == 1 and "\u3400" <= item <= "\u9fff"]
    tokens.update(left + right for left, right in itertools.pairwise(han))
    return frozenset(tokens)


__all__ = ["normalize_text", "tokenize"]
