"""Tunable settings, kept out of the code.

Resolution order (later wins):
  1. defaults below
  2. settings file: $CODEGRAPH_SETTINGS, else <CODEGRAPH_DATA_DIR>/settings.yaml
  3. environment variables (CODEGRAPH_LLM_MIN_SCORE, ...)
  4. per-run parameters (API `params`, CLI flags)

Only the AI-judgement budget lives here for now:

  llm:
    min_score: 0.3        # candidates scoring below this are judged by rules, not the AI
    max_items: 300        # AI-judged candidates per run; "unlimited" (or -1) = no cap,
                          # 0 = no AI judgement (the requirement is still structured by the AI once)
    reuse_verdicts: true  # re-investigations keep the parent's AI verdicts instead of asking again
    rescore_delta: 0.1    # ...unless a candidate's score rose by at least this much (new evidence)
    batch_size: 15        # candidates per AI request
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace

import yaml

UNLIMITED_WORDS = {"unlimited", "none", "no", "off", "inf", "infinite", "無制限", "上限なし"}


def parse_max_items(v) -> int | None:
    """None = no cap. Accepts ints, numeric strings, and words meaning 'unlimited'."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError("max_items must be a number or 'unlimited'")
    if isinstance(v, (int, float)):
        n = int(v)
    else:
        s = str(v).strip().lower()
        if s in UNLIMITED_WORDS:
            return None
        n = int(float(s))
    if n < 0:
        return None
    return n


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class LLMBudget:
    min_score: float = 0.3
    max_items: int | None = 300
    reuse_verdicts: bool = True
    rescore_delta: float = 0.1
    batch_size: int = 15

    def with_overrides(self, d: dict | None) -> "LLMBudget":
        """Apply overrides given with plain keys (min_score, max_items, ...)."""
        if not d:
            return self
        b = self
        if d.get("min_score") is not None:
            b = replace(b, min_score=_score(d["min_score"]))
        if "max_items" in d:
            b = replace(b, max_items=parse_max_items(d["max_items"]))
        if d.get("reuse_verdicts") is not None:
            b = replace(b, reuse_verdicts=_bool(d["reuse_verdicts"]))
        if d.get("rescore_delta") is not None:
            b = replace(b, rescore_delta=float(d["rescore_delta"]))
        if d.get("batch_size") is not None:
            b = replace(b, batch_size=max(1, int(d["batch_size"])))
        return b

    def with_run_params(self, params: dict | None) -> "LLMBudget":
        """Per-run overrides: an `llm` block and/or llm_-prefixed keys (llm_max_items ...).
        Plain keys are not read here: a run's `min_score` is the propagation cut-off."""
        if not params:
            return self
        d = dict(params.get("llm") or {})
        d.update({k[4:]: v for k, v in params.items() if k.startswith("llm_")})
        return self.with_overrides(d)

    def to_dict(self) -> dict:
        return {"min_score": self.min_score, "max_items": "unlimited" if self.max_items is None else self.max_items,
                "reuse_verdicts": self.reuse_verdicts, "rescore_delta": self.rescore_delta,
                "batch_size": self.batch_size}


def _score(v) -> float:
    f = float(v)
    if not 0.0 <= f <= 1.0:
        raise ValueError("min_score must be between 0 and 1")
    return f


def settings_path(data_dir: str | None = None) -> str | None:
    p = os.environ.get("CODEGRAPH_SETTINGS")
    if p:
        return p
    d = data_dir or os.environ.get("CODEGRAPH_DATA_DIR")
    return os.path.join(d, "settings.yaml") if d else None


def load_llm_budget(data_dir: str | None = None) -> LLMBudget:
    b = LLMBudget()
    p = settings_path(data_dir)
    if p and os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        b = b.with_overrides(data.get("llm") or {})
    env = {}
    for k in ("min_score", "max_items", "reuse_verdicts", "rescore_delta", "batch_size"):
        v = os.environ.get(f"CODEGRAPH_LLM_{k.upper()}")
        if v is not None and v.strip() != "":
            env[k] = v
    return b.with_overrides(env)
