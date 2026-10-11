"""TypeScript / Next.js / Supabase / Prisma: extraction, module resolution, and a
Japanese requirement reaching code through screen wording."""
import os
import subprocess

import pytest

from himotoki.analysis.engine import ja_terms
from himotoki.workspace import Workspace

FILES = {
    "tsconfig.json": '{ "compilerOptions": { "paths": { "@/*": ["./web/src/*"] } } // comment\n}',
    "packages/shared/package.json": '{ "name": "@acme/shared", "exports": { "./money": { "types": "./src/money.ts" } } }',
    "packages/shared/src/money.ts": "/** 金額を円表記にする */\nexport function formatYen(v: number): string { return `${v}円`; }\n",
    "prisma/schema.prisma": """
/// 寄付
model Donation {
  id        String @id
  /// 寄付者名
  donorName String @map("donor_name")
  amount    Int
  @@map("donations")
}
""",
    "web/src/lib/index.ts": 'export * from "./donations";\nexport { formatDate as fmtDate } from "./dates";\n',
    "web/src/lib/dates.ts": "export const formatDate = (d: Date) => d.toISOString();\n",
    "web/src/lib/donations.ts": """
import { formatYen } from "@acme/shared/money";
import { prisma } from "./prisma";
export interface DonationRepository { list(): Promise<string[]>; }
export class PrismaDonationRepository implements DonationRepository {
  async list() {
    const rows = await prisma.donation.findMany({});
    return rows.map((r) => formatYen(r.amount));
  }
}
export async function hideDonor(id: string) {
  await supabase.from("donations").update({ donor_name: null }).eq("id", id);
}
""",
    "web/src/lib/prisma.ts": "export const prisma = {} as any;\n",
    "web/src/components/DonationRow.tsx": """
export function DonationRow({ name }: { name: string }) {
  return <tr><td>{name}</td></tr>;
}
""",
    "web/src/components/DonationTable.tsx": """
import { DonationRow } from "./DonationRow";
import { fmtDate, PrismaDonationRepository } from "@/lib";
export default function DonationTable() {
  const repo = new PrismaDonationRepository();
  return <table><caption>寄付者一覧</caption><DonationRow name={fmtDate(new Date())} /></table>;
}
""",
    "web/src/app/(public)/donations/[year]/page.tsx": """
import DonationTable from "@/components/DonationTable";
export default async function Page() { return <DonationTable />; }
""",
    "web/src/app/api/donations/route.ts": """
import { hideDonor } from "@/lib/donations";
export async function POST(req: Request) { await hideDonor("1"); return new Response("ok"); }
""",
    "web/src/lib/donations.test.ts": """
import { hideDonor } from "./donations";
describe("hideDonor", () => { it("寄付者名を隠す", async () => { await hideDonor("1"); }); });
""",
}


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    root = tmp_path_factory.mktemp("ts")
    repo = root / "repo"
    for path, body in FILES.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    ws = Workspace(str(root / "ws"))
    stats = ws.index(str(repo), name="ts")
    return ws.engine("ts"), stats


def _edges(e, src, rel):
    return {x.dst for x in e.store.out_edges(src, [rel])}


def test_modules_resolve_through_aliases_packages_and_barrels(engine):
    e, stats = engine
    assert stats["errors"] == 0
    table = "ts:web/src/components/DonationTable.tsx#DonationTable"
    # tsconfig alias + barrel `export *` + renamed re-export
    assert "ts:web/src/lib/donations.ts#PrismaDonationRepository" in _edges(e, table, "REFERENCES")
    assert "ts:web/src/lib/dates.ts#formatDate" in _edges(e, table, "CALLS")
    # workspace package `exports`
    assert "ts:packages/shared/src/money.ts#formatYen" in _edges(
        e, "ts:web/src/lib/donations.ts#PrismaDonationRepository.list", "CALLS")
    # JSX components are rendered, not called
    assert "ts:web/src/components/DonationRow.tsx#DonationRow" in _edges(e, table, "RENDERS")
    # library imports are dependencies, not gaps
    assert not stats["unresolved"].get("tsimp") and not stats["unresolved"].get("tsexp")


def test_data_access_interfaces_routes_and_tests(engine):
    e, _ = engine
    lib = "ts:web/src/lib/donations.ts"
    assert "table:donations" in _edges(e, f"{lib}#PrismaDonationRepository.list", "READS")   # prisma.donation
    assert "table:donations" in _edges(e, f"{lib}#hideDonor", "WRITES")                     # supabase update
    assert f"{lib}#PrismaDonationRepository.list" in _edges(e, f"{lib}#DonationRepository.list", "IMPLEMENTED_BY")
    assert e.store.get("col:donations.donor_name") is not None
    page = e.store.get("ep:PAGE /donations/{year}")
    api = e.store.get("ep:POST /api/donations")
    assert page is not None and api is not None
    testers = {x.src for x in e.store.in_edges(f"{lib}#hideDonor", ["TESTS"])}
    assert any(e.store.get(t).kind == "TestMethod" and "寄付者名を隠す" in e.store.get(t).name for t in testers)


def test_japanese_requirement_reaches_screen_by_its_wording(engine):
    e, _ = engine
    assert "寄付者一覧" in ja_terms("「寄付者一覧」に金額の列を追加する")
    rid = e.start_run("「寄付者一覧」に金額の列を追加する", background=False)
    c = e.state(rid)["candidates"]
    table = "ts:web/src/components/DonationTable.tsx#DonationTable"
    assert table in c and c[table]["origin"] == "seed"
    assert "寄付者一覧" in c[table]["seed_reason"]
    # the row it renders and the page that renders it follow
    assert "ts:web/src/components/DonationRow.tsx#DonationRow" in c
    assert any(k.startswith("ts:web/src/app/(public)/donations") for k in c)
