import { useState } from "react";
import type { Candidate } from "./types";

/** Words a beginner may not know, explained where they appear (hover or focus). */
export const GLOSSARY: Record<string, string> = {
  確度: "その要素が本当に影響を受ける確からしさ（0〜1）。つながりの強さと根拠の確かさから計算し、AI判定があればその確信度を使います。",
  全数判定: "「これで影響箇所は全部」と言えるかを確かめる条件の一覧。依存のたどり残し・横断検索・独立した再探索・テーブルの読み書き全箇所を確認し、すべて満たすと承認できます。",
  未判定: "まだ人が「確定」も「除外」もしていない候補。",
  規模: "確定した要素の工数と、未判定の要素の工数×確からしさを足した見込み（人日）。括弧内は幅。",
  取込状況: "ソースや設計書をどこまで読み取れたか。読めなかった参照は、影響調査の死角になります。",
  学習: "このシステムで人が確定・除外した結果と、実装との照合結果から学んだ補正。",
  伝播: "変更の影響が、呼び出しや読み書きなどのつながりをたどって広がること。",
  CRUD: "テーブルへの登録（Create）・参照（Read）・更新（Update）・削除（Delete）。",
};

export function Term({ k, children }: { k: keyof typeof GLOSSARY | string; children?: React.ReactNode }) {
  const tip = GLOSSARY[k];
  if (!tip) return <>{children ?? k}</>;
  return <span className="term" title={tip} tabIndex={0} aria-label={`${k}: ${tip}`}>{children ?? k}</span>;
}

const HINTS_KEY = "himotoki.hints.v1";

function seenHints(): Record<string, boolean> {
  try { return JSON.parse(localStorage.getItem(HINTS_KEY) || "{}"); } catch { return {}; }
}

/** A one-time hint shown where (and when) the thing it explains is first used. */
export function Hint({ id, children }: { id: string; children: React.ReactNode }) {
  const [hidden, setHidden] = useState(() => { const s = seenHints(); return !!(s.all || s[id]); });
  if (hidden) return null;
  const close = () => {
    setHidden(true);
    try { localStorage.setItem(HINTS_KEY, JSON.stringify({ ...seenHints(), [id]: true })); } catch { /* storage blocked */ }
  };
  return (
    <div className="hint" role="note">
      <span className="hint-mark" aria-hidden="true">ヒント</span>
      <span className="hint-body">{children}</span>
      <button className="hint-close" onClick={close} aria-label="このヒントを閉じる">わかった</button>
    </div>
  );
}

/** How a verdict was reached, said honestly (rule-based judgement is never labelled AI). */
export function judgedSuffix(c: Candidate): string {
  if (c.origin === "human") return "";
  switch (c.judged_by) {
    case "ai": return "（AI）";
    case "reused": return "（AI・前回）";
    case null: case undefined: return "";
    default: return "（ルール）";
  }
}
