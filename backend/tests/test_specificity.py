"""Word specificity: common identifier words weigh less, compound names are found."""
from himotoki.analysis.engine import Engine, compounds


def _engine(n, df):
    e = Engine.__new__(Engine)
    e._terms = {"n": n, "df": df, "names": [], "sub": {}}
    return e


def test_specificity_follows_system_size():
    big = _engine(100_000, {"loan": 9000, "fraud": 8, "breach": 300})
    assert big.specificity("loan") == 0.0          # used everywhere: says nothing
    assert big.specificity("fraud") == 1.0         # rare: decisive
    assert 0.3 < big.specificity("breach") < 0.8
    small = _engine(300, {"owner": 30})
    assert small.specificity("owner") > 0.3       # in a small system the same count still matters


def test_compounds_match_existing_identifiers():
    by_name = {"workingcapitalloan": [1], "markasfraud": [2], "markfraud": [3]}
    out = compounds("Working Capital loan mark as fraud", by_name)
    assert "workingcapitalloan" in out and "markasfraud" in out and "markfraud" in out
    assert compounds("nothing here", by_name) == []
