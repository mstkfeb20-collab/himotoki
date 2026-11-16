"""Text helpers shared by indexers and search."""
from __future__ import annotations

import re

def decode_source(data: bytes) -> str:
    """Source files in Japanese systems are often Shift_JIS (Windows-31J). UTF-8 first,
    then CP932, then UTF-8 with replacement characters."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for enc in ("utf-8", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


def read_source(path: str) -> str:
    with open(path, "rb") as fh:
        return decode_source(fh.read())


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


_ID_RX = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,8}(?:[-_][A-Za-z]{1,8}){0,2}[-_]?\d{2,6}(?:[-_][A-Za-z]{0,3}\d{1,4})*(?:[A-Z](?![a-z]))?)(?![0-9])")


def system_ids(text: str) -> list[str]:
    """ID-like tokens (letters then digits: KOB041, HZK-G010, IF_ZAI_005), normalised to
    upper case without separators so that every spelling meets."""
    out = []
    for m in _ID_RX.finditer(text or ""):
        tok = re.sub(r"[-_]", "", m.group(1)).upper()
        letters = sum(c.isalpha() for c in tok)
        if letters >= 2 and len(tok) >= 5 and tok not in _ID_STOP and tok not in out:
            out.append(tok)
    return out


_ID_STOP = {"UTF8", "UTF16", "SHA256", "SHA512", "MD5", "BASE64", "ISO8859", "HTTP11", "SJIS", "WIN31", "X509",
            "LOG4J", "JUNIT4", "JUNIT5", "JAVA11", "JAVA17", "JAVA8"}


def datafile_base(v: str) -> str:
    """The name that identifies an exchanged file whatever its directory, variables and
    date stamp: ${OUT}/URIAGE_${YMD}.dat, /data/URIAGE_20260401.dat -> uriage.dat."""
    base = re.sub(r"\$\{?\w+\}?|%\w+%|%[\d$\-]*[sd]|\{\d+\}", "", v.strip().strip("\"'")).rsplit("/", 1)[-1].lower()
    base = re.sub(r"(yyyy|yy)(mm)?(dd)?|\d{6,14}|\*", "", base)
    base = re.sub(r"[_\-.]{2,}", lambda m: m.group(0)[-1], base)
    base = re.sub(r"[_\-]+\.", ".", base).strip("_-")
    return base
