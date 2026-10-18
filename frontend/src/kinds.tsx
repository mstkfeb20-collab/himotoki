/** What an element is, at a glance: program, screen, DB, job, interface file, design
 *  document, test, message or setting. Shown as an icon + word, never by colour alone
 *  (colours already mean judgement status elsewhere in the viewer). */

export type KindClass = "code" | "screen" | "sql" | "db" | "job" | "file" | "doc" | "test" | "msg" | "config";

const CLASS_OF: Record<string, KindClass> = {
  Class: "code", Method: "code", Field: "code", Bean: "code", SqlMapper: "code", Feature: "code",
  Template: "screen", Endpoint: "screen",
  SqlStatement: "sql", RuntimeSql: "sql",
  Table: "db", Column: "db", SqlFile: "db", DbObject: "db",
  Job: "job", JobNet: "job", CronEntry: "job", BatchJob: "job", Script: "job",
  DataFile: "file",
  DocSection: "doc", DocEntity: "doc",
  TestClass: "test", TestMethod: "test",
  MessageKey: "msg",
  ConfigFile: "config", XmlElement: "config",
};
export const CLASS_LABEL: Record<KindClass, string> = {
  code: "プログラム", screen: "画面", sql: "SQL", db: "DB", job: "ジョブ・シェル", file: "連携ファイル",
  doc: "設計書", test: "テスト", msg: "メッセージ", config: "設定",
};
export const KIND_SHORT: Record<string, string> = {
  Class: "クラス", Method: "メソッド", Field: "項目", Bean: "Bean", SqlMapper: "マッパー", Template: "画面",
  Endpoint: "URL", SqlStatement: "SQL文", RuntimeSql: "実行時SQL", Table: "テーブル", Column: "列", SqlFile: "SQLファイル",
  DbObject: "DBオブジェクト", Job: "ジョブ", JobNet: "ジョブネット", CronEntry: "cron", BatchJob: "バッチ", Script: "シェル",
  DataFile: "連携ファイル", DocSection: "設計書の節", DocEntity: "設計書の行", TestClass: "テスト", TestMethod: "テスト",
  MessageKey: "メッセージ", ConfigFile: "設定ファイル", XmlElement: "XML定義", Feature: "機能",
};

export function kindClass(kind: string, file?: string | null): KindClass {
  const c = CLASS_OF[kind];
  if (c) return c;
  if (file && /\.(xlsx|xlsm|docx|md)$/i.test(file)) return "doc";
  return "code";
}

/** the first folder of the path: the subsystem / server in a monorepo */
export function subsystem(file?: string | null): string | null {
  if (!file) return null;
  const parts = file.replace(/^design:/, "docs/").split("/");
  return parts.length > 1 ? parts[0] : null;
}

const PATHS: Record<KindClass, string> = {
  code: "M5.5 4 2 8l3.5 4M10.5 4 14 8l-3.5 4",                                 // < >
  screen: "M2 3.5h12v8H2zM2 6h12M6 14h4",                                       // window
  sql: "M3 4h10M3 8h7M3 12h10",                                                 // lines of a statement
  db: "M3 4c0-1.1 2.2-2 5-2s5 .9 5 2-2.2 2-5 2-5-.9-5-2zm0 0v8c0 1.1 2.2 2 5 2s5-.9 5-2V4M3 8c0 1.1 2.2 2 5 2s5-.9 5-2",
  job: "M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2zm0 3v3.5l2.5 1.5",                    // clock
  file: "M2 5h9l3 3-3 3H2zM5 8h6",                                              // hand-over arrow
  doc: "M4 2h5.5L12 4.5V14H4zM9.5 2v2.5H12M6 8h4M6 10.5h4",                     // sheet
  test: "M3 8.5 6.5 12 13 4.5",                                                 // check
  msg: "M2.5 3h11v7.5H7l-3 2.5v-2.5H2.5z",                                      // balloon
  config: "M3 4h10M3 8h10M3 12h10M6 2.5v3M10 6.5v3M7 10.5v3",                   // sliders
};

export function KindIcon({ cls, size = 14 }: { cls: KindClass; size?: number }) {
  return (
    <svg className={`kicon k-${cls}`} width={size} height={size} viewBox="0 0 16 16" aria-hidden="true"
      fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d={PATHS[cls]} />
    </svg>
  );
}

/** icon + class word; the exact kind on hover */
export function KindChip({ kind, file, compact = false }: { kind: string; file?: string | null; compact?: boolean }) {
  const cls = kindClass(kind, file);
  return (
    <span className={`kchip k-${cls}${compact ? " compact" : ""}`} title={`${CLASS_LABEL[cls]}（${KIND_SHORT[kind] || kind}）`}>
      <KindIcon cls={cls} />{!compact && <span>{CLASS_LABEL[cls]}</span>}
    </span>
  );
}
