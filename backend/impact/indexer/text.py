"""Text helpers shared by indexers and search."""
from __future__ import annotations

import re

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def split_ident(name: str) -> list[str]:
    """`findByLastNameStartingWith` -> ['find', 'by', 'last', 'name', 'starting', 'with']"""
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        out.extend(w.lower() for w in _CAMEL.findall(part))
    return out


def snake(name: str) -> str:
    return "_".join(split_ident(name))


def words(name: str) -> str:
    return " ".join(split_ident(name))


def singular(word: str) -> str:
    w = word
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("sses"):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w
