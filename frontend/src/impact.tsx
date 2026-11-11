import type { Candidate } from "./types";

/** Kinds of impact (backend: analysis/impact.py). Told apart by word and outline style,
 *  never by colour alone: colours already mean the judgement status. */
export type Impact = "change" | "verify" | "coordinate" | "none" | "unknown";

export const IMPACT_JA: Record<Impact, string> = {
  change: "修正", verify: "試験", coordinate: "調整", none: "影響なし", unknown: "判断できず",
};
export const IMPACT_HELP: Record<Impact, string> = {
  change: "中身を書き換える（コード・SQL・画面・レイアウト・設計書・データ）",
  verify: "中身は変えないが、つながる先が変わるので試験で動作を確かめる",
  coordinate: "コードは変えないが、他チーム・他システム・運用手順と決めることがある",
  none: "つながってはいるが、今回の変更とは関係しない",
  unknown: "材料が足りず判断できない",
};
export const AFFECTING: Impact[] = ["change", "verify", "coordinate"];
export const EVIDENCE_JA: Record<string, string> = { where: "直す箇所", check: "試験の観点", with: "調整先・内容", why: "理由", basis: "根拠" };

/** The kind that counts: the reviewer's when decided, the judge's otherwise. */
export function effectiveImpact(c: Candidate): Impact | null {
  const d = c.decision;
  if (d?.status === "excluded") return "none";
  if (d?.status === "confirmed") {
    const k = (d.impact || c.impact) as Impact | undefined;
    return k && AFFECTING.includes(k) ? k : "change";
  }
  return (c.impact as Impact) || null;
}

export function ImpactTag({ kind, small = false }: { kind: Impact | null; small?: boolean }) {
  if (!kind) return null;
  return <span className={`itag i-${kind}${small ? " small" : ""}`} title={IMPACT_HELP[kind]}>{IMPACT_JA[kind]}</span>;
}
