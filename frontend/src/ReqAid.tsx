import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { KIND_JA } from "./ListView";

/** Templates for the kinds of change business users usually write. */
export const TEMPLATES: { label: string; text: string; hint: string }[] = [
  { label: "値・桁数の変更", hint: "入力チェックや上限値、件数などを変える",
    text: "【対象】〇〇画面の△△（項目名）\n【変更】入力できる文字数の上限を 80文字 から 200文字 に変更する\n【関連】帳票・CSV出力・設計書の記載も合わせる" },
  { label: "項目の追加", hint: "画面やテーブルに新しい項目を持たせる",
    text: "【対象】〇〇（例: 顧客）に △△（例: 携帯電話番号）を登録できるようにする\n【表示】〇〇画面と△△一覧に表示する\n【制約】必須ではない。半角数字10〜11桁" },
  { label: "項目・機能の削除", hint: "使わなくなった項目や機能をなくす",
    text: "【対象】〇〇画面の△△（項目名）を削除する\n【データ】テーブルの△△列は残す／削除する\n【関連】帳票・バッチからも外す" },
  { label: "処理・条件の変更", hint: "計算・判定・検索条件などのルールを変える",
    text: "【対象】〇〇処理（例: 予約の取消）\n【変更】△△の条件を「出発の3日前まで」から「出発の7日前まで」に変更する\n【影響】画面のメッセージも合わせる" },
  { label: "バッチ・連携の変更", hint: "夜間ジョブや連携ファイルを変える",
    text: "【対象】夜間ジョブ 〇〇（例: 予約データ出力）\n【変更】出力ファイルに △△ 列を追加する\n【連携先】受け取り側のジョブ・取込処理も確認する" },
];

type Preview = {
  intents: { id: string; summary: string; change_type: string; keywords: string[]; new_terms: string[] }[];
  seeds: { node_id: string; name: string; kind: string; score: number; reason?: string }[];
  seed_count: number; strong_seeds: number; warnings: string[]; tips: string[];
};
const CT_JA: Record<string, string> = { add: "追加", modify: "変更", delete: "削除" };

/** Writing aid next to the requirement box: templates, and a live check of how the
 * requirement is read and what it would start from (rule-based, no AI tokens). */
export default function ReqAid({ text, setText, graph }: { text: string; setText: (t: string) => void; graph?: string | null }) {
  const [pv, setPv] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(() => {
    if (timer.current) window.clearTimeout(timer.current);
    if (!text.trim()) { setPv(null); return; }
    timer.current = window.setTimeout(() => {
      setBusy(true);
      api.previewRequirement(text, graph).then(setPv).catch(() => setPv(null)).finally(() => setBusy(false));
    }, 700);
    return () => { if (timer.current) window.clearTimeout(timer.current); };
  }, [text, graph]);

  const level = !pv ? "none" : pv.warnings.length ? "warn" : pv.tips.length ? "tip" : "ok";
  return (
    <div className="reqaid">
      <div className="tpl-row">
        <span className="k">テンプレート</span>
        {TEMPLATES.map((t) => (
          <button key={t.label} type="button" className="chip" title={t.hint}
            onClick={() => setText(text.trim() ? `${text.trim()}\n\n${t.text}` : t.text)}>{t.label}</button>
        ))}
      </div>
      <p className="muted tiny">書き方のコツ: 「どの画面・帳票・バッチの」「どの項目を」「何から何に」の3点を入れる。変更前の値（例: 80文字から）があると、今の値を使っている箇所まで探せます。1つの要求には1つの変更を。</p>
      {text.trim() && (
        <div className={`pv lvl-${level}`} aria-live="polite">
          <div className="pv-head">
            <span className="k">この書き方での読み取り（AIは使いません）</span>
            {busy && <span className="muted tiny">確認中…</span>}
          </div>
          {pv && (
            <>
              {pv.warnings.map((w, i) => <p key={`w${i}`} className="pv-warn">⚠ {w}</p>)}
              {pv.tips.map((t, i) => <p key={`t${i}`} className="pv-tip">💡 {t}</p>)}
              {pv.intents.length > 0 && (
                <ul className="pv-intents">
                  {pv.intents.map((it) => (
                    <li key={it.id}><span className="tag">{CT_JA[it.change_type] || it.change_type}</span> {it.summary}
                      {it.keywords.length > 0 && <span className="muted tiny">　手がかり: {it.keywords.slice(0, 6).join("・")}</span>}</li>
                  ))}
                </ul>
              )}
              <div className="pv-seeds">
                <span className="muted small">起点になりそうな要素 {pv.seed_count} 件{pv.strong_seeds ? `（名前・値が一致 ${pv.strong_seeds} 件）` : ""}:</span>
                {pv.seeds.slice(0, 8).map((s) => (
                  <span key={s.node_id} className="seed" title={s.reason || ""}>{s.name}<small> {KIND_JA[s.kind] || s.kind}</small></span>
                ))}
              </div>
              {level === "ok" && <p className="pv-ok">✓ 対象が特定できています。このまま解析を開始できます。</p>}
            </>
          )}
        </div>
      )}
    </div>
  );
}
