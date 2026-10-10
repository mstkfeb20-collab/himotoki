"""LLM adapter. With ANTHROPIC_API_KEY set, requirement structuring, node
judgement and free-form questions go to Claude; otherwise a deterministic
rule-based fallback keeps the whole pipeline usable (and testable) offline."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from ..model import Kind

UNIT = r"(?:件|桁|文字|日|個|回|円|％|%|分|秒|時間|歳|行|ページ)?"
OLD_VALUE_RX = re.compile(r"(\d+)\s*" + UNIT + r"\s*(?:固定)?\s*から")
NEW_VALUE_RX = re.compile(r"(\d+)\s*" + UNIT + r"\s*(?:に|へ)(?:変更|拡張|縮小|増や|減ら)")
CHANGE_VALUE_RX = re.compile(r"から.{0,40}(?:に|へ|を).{0,12}(?:変更|拡張|縮小|許容|増や|減ら)")
ADD_RX = re.compile(r"追加|新設|新た|登録できる|入力できる|保持|持たせ|できるようにする|\badd\b|\bnew\b|\bintroduce", re.I)
DEL_RX = re.compile(r"削除|廃止|不要|撤去|\bremove\b|\bdelete\b|\bdrop\b", re.I)

# Small generic glossary for terms a message bundle may not contain.
GLOSSARY = {
    "メールアドレス": "email", "メール": "email", "電話": "telephone phone", "詳細": "details",
    "連絡先": "contact", "携帯": "mobile", "備考": "remarks note", "性別": "gender", "体重": "weight",
    "住所": "address", "郵便番号": "zip postal code", "生年月日": "birth date", "誕生日": "birth date",
    "名前": "name", "氏名": "first name last name", "姓": "last name", "名": "first name",
    "検索": "find search", "一覧": "list", "登録": "create new save", "更新": "update edit",
    "削除": "delete remove", "画面": "form view", "項目": "field column",
    "必須": "required not blank", "桁": "length size digits", "文字数": "length size",
    "日付": "date", "予約": "visit appointment", "診察": "visit", "獣医": "vet veterinarian",
    "専門": "specialty", "ペット": "pet", "飼い主": "owner", "オーナー": "owner", "種類": "type",
    "説明": "description", "ページ": "page pagination", "件数": "count page size",
    "バリデーション": "validation validator", "入力チェック": "validation validator",
    "都市": "city", "市区町村": "city", "エラー": "error", "メッセージ": "message",
}


def _extract_json(text: str) -> Any:
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    raw = m.group(1) if m else text
    start = min([i for i in (raw.find("{"), raw.find("[")) if i >= 0] or [0])
    return json.loads(raw[start:])


log = logging.getLogger("codegraph.llm")

class RuleBasedLLM:
    name = "rule-based"
    available = False

    def __init__(self, vocabulary: dict[str, list[str]] | None = None, identifiers: set[str] | None = None):
        # vocabulary: surface text (any locale) -> identifiers (e.g. 電話番号 -> ['telephone'])
        # identifiers: lower-cased identifier words that exist in the code base
        self.vocab = vocabulary or {}
        self.identifiers = identifiers or set()

    def structure_requirement(self, text: str, meter: dict | None = None) -> list[dict]:
        clauses = [c.strip() for c in re.split(r"[。\n]|(?<=[しり])、|。", text) if c.strip()]
        if not clauses:
            clauses = [text]
        intents = []
        for c in clauses:
            change = "add" if ADD_RX.search(c) else "delete" if DEL_RX.search(c) else "modify"
            known, new_terms = [], []
            consumed = c
            for surface in sorted(self.vocab, key=len, reverse=True):
                if len(surface) >= 2 and surface in consumed:
                    known += self.vocab[surface]
                    consumed = consumed.replace(surface, " ")
            for ja, en in sorted(GLOSSARY.items(), key=lambda kv: -len(kv[0])):
                if ja in consumed:
                    terms = en.split()
                    hit = [t for t in terms if t in self.identifiers]
                    known += hit
                    if not hit:
                        new_terms += terms
                    consumed = consumed.replace(ja, " ")
            ascii_terms = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", c)
            known += ascii_terms
            quoted = re.findall(r"「([^」]+)」", c)
            values = {"old": OLD_VALUE_RX.findall(c), "new": NEW_VALUE_RX.findall(c)}
            value_change = bool(values["old"]) or bool(values["new"]) or bool(CHANGE_VALUE_RX.search(c))
            if value_change and not re.search(r"追加|新設|新た|登録できる", c):
                change = "modify"   # "AからBに変更": an existing value or rule changes
                new_terms = []
            elif new_terms and change == "modify":
                change = "add"   # a concept that does not exist yet has to be introduced
            intents.append({
                "summary": c,
                "change_type": change,
                "keywords": sorted(set(known)),
                "new_terms": sorted(set(new_terms + [q for q in quoted if q not in self.vocab])),
                "values": values,
            })
        return intents

    def judge(self, intent: dict, items: list[dict], batch_size: int = 15, meter: dict | None = None) -> list[dict]:
        out = []
        for it in items:
            s = it["score"]
            # derived tests and documents need stronger evidence than code to be called impacted
            hi = 0.6 if it["kind"] in (Kind.TEST_CLASS, Kind.TEST_METHOD, Kind.DOC) else 0.5
            verdict = "impacted" if s >= hi else "uncertain" if s >= 0.3 else "not_impacted"
            out.append({"id": it["id"], "verdict": verdict, "confidence": round(min(0.95, s), 2),
                        "reason": it["path_reason"], "change_type": it.get("change_type", "modify"), "_by": "rule"})
        return out

    def answer(self, question: str, context: str, meter: dict | None = None) -> str:
        return ("LLMが未設定のため自由質問には回答できません（ANTHROPIC_API_KEY を設定すると有効になります）。"
                "関連する根拠情報:\n" + context[:1500])


class ClaudeLLM(RuleBasedLLM):
    name = "claude"
    available = True

    def __init__(self, vocabulary=None, identifiers=None):
        super().__init__(vocabulary, identifiers)
        import anthropic
        # bounded waits: a slow or unreachable API must not stall a run indefinitely;
        # failures fall back to the rule-based answer (see the except branches below)
        self.client = anthropic.Anthropic(timeout=float(os.environ.get("CODEGRAPH_LLM_TIMEOUT", "90")), max_retries=2)
        self.model = os.environ.get("CODEGRAPH_LLM_MODEL", "claude-sonnet-5-5")

    def _ask(self, system: str, user: str, max_tokens: int = 4000, meter: dict | None = None) -> str:
        msg = self.client.messages.create(model=self.model, max_tokens=max_tokens, system=system,
                                          messages=[{"role": "user", "content": user}])
        if meter is not None:
            # actual usage reported by the API (not an estimate)
            u = getattr(msg, "usage", None)
            meter["calls"] = meter.get("calls", 0) + 1
            meter["input_tokens"] = meter.get("input_tokens", 0) + int(getattr(u, "input_tokens", 0) or 0)
            meter["output_tokens"] = meter.get("output_tokens", 0) + int(getattr(u, "output_tokens", 0) or 0)
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def structure_requirement(self, text: str, meter: dict | None = None) -> list[dict]:
        hint = super().structure_requirement(text)
        system = ("You decompose software change requirements into change intents for impact analysis. "
                  "Answer with JSON only: {\"intents\":[{\"summary\":str (in the requirement's language),"
                  "\"change_type\":\"add|modify|delete\",\"keywords\":[identifier-like English words likely "
                  "used in code],\"new_terms\":[concepts that do not exist yet]}]}")
        vocab_sample = ", ".join(f"{k}->{'/'.join(v)}" for k, v in list(self.vocab.items())[:150])
        try:
            data = _extract_json(self._ask(system, f"Requirement:\n{text}\n\nKnown vocabulary (UI label -> "
                                                    f"identifier): {vocab_sample}\n\nRule-based draft: "
                                                    f"{json.dumps(hint, ensure_ascii=False)}", meter=meter))
            intents = data.get("intents") or hint
            for i in intents:
                i.setdefault("keywords", []); i.setdefault("new_terms", []); i.setdefault("change_type", "modify")
            return intents
        except Exception:
            log.warning("Claude API call failed; using the rule-based structuring", exc_info=True)
            return hint

    def judge(self, intent: dict, items: list[dict], batch_size: int = 15, meter: dict | None = None) -> list[dict]:
        base = {r["id"]: dict(r, _by="rule:fallback") for r in super().judge(intent, items)}
        system = ("You judge whether code elements must change to implement a requirement intent. "
                  "For each item answer JSON only: {\"results\":[{\"id\":str,\"verdict\":"
                  "\"impacted|uncertain|not_impacted\",\"confidence\":0-1,\"reason\":str (Japanese, one "
                  "sentence),\"change_type\":\"add|modify|delete|test|doc\"}]}")
        results = []
        step = max(1, int(batch_size))
        for i in range(0, len(items), step):
            chunk = items[i:i + step]
            body = json.dumps([{k: it[k] for k in ("id", "kind", "name", "path_reason", "snippet")}
                               for it in chunk], ensure_ascii=False)
            try:
                data = _extract_json(self._ask(system, f"Intent: {intent['summary']} "
                                                        f"(change type: {intent['change_type']})\nItems:\n{body}",
                                               meter=meter))
                for r in data.get("results", []):
                    if r.get("id") in base:
                        results.append({**base[r["id"]], **r, "_by": "ai"})
            except Exception:
                log.warning("Claude judgement failed for a chunk; keeping rule-based verdicts", exc_info=True)
                results += [base[it["id"]] for it in chunk]
        seen = {r["id"] for r in results}
        return results + [r for k, r in base.items() if k not in seen]

    def answer(self, question: str, context: str, meter: dict | None = None) -> str:
        try:
            return self._ask("You are an impact-analysis assistant. Answer in Japanese, concisely, citing the "
                             "given context (file:line).", f"Context:\n{context}\n\nQuestion: {question}", 1500,
                             meter=meter)
        except Exception:
            log.warning("Claude API call failed; answering without it", exc_info=True)
            return "Claude API に接続できなかったため回答できません。関連する根拠情報:\n" + context[:1500]


def make_llm(vocabulary: dict[str, list[str]], identifiers: set[str] | None = None):
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return ClaudeLLM(vocabulary, identifiers)
        except Exception:
            pass
    return RuleBasedLLM(vocabulary, identifiers)
