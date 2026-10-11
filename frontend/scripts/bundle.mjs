// Copies the built viewer into the Python package (backend/codegraph/web) so that
// `pip install` + `codegraph serve` works without Node.js. Records a hash of the
// viewer sources; a backend test fails when the bundle is older than the sources.
import { createHash } from "node:crypto";
import { cpSync, existsSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(root, "..", "backend", "codegraph", "web");

export function sourceHash() {
  const files = [];
  const walk = (d) => {
    for (const n of readdirSync(d)) {
      const p = join(d, n);
      if (statSync(p).isDirectory()) walk(p); else files.push(p);
    }
  };
  walk(join(root, "src"));
  for (const f of ["index.html", "package.json"]) files.push(join(root, f));
  const rel = files.map((p) => [relative(root, p).split("\\").join("/"), p]).sort((a, b) => (a[0] < b[0] ? -1 : 1));
  const h = createHash("sha256");
  for (const [r, p] of rel) {
    h.update(r + "\0");
    h.update(readFileSync(p, "utf8").replace(/\r\n/g, "\n"));
    h.update("\0");
  }
  return h.digest("hex");
}

if (!existsSync(join(root, "dist", "index.html"))) {
  console.error("dist/ not found: run `npm run build` first");
  process.exit(1);
}
rmSync(out, { recursive: true, force: true });
cpSync(join(root, "dist"), out, { recursive: true });
writeFileSync(join(out, "BUILD_INFO.json"), JSON.stringify({ source_hash: sourceHash() }, null, 1) + "\n");
console.log(`viewer bundled into ${relative(join(root, ".."), out)}`);
