"""himotoki alone, rule-based: the analysis a viewer run would show, for one task."""
import json, os, sys
ws, req, out = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ.update({"HIMOTOKI_DATA_DIR": ws, "HIMOTOKI_STORE": "local", "ANTHROPIC_API_KEY": ""})
for k in list(os.environ):
    if k.startswith("HIMOTOKI_LLM_"):
        del os.environ[k]
from himotoki import mcp_server as M
r = M.analyze_requirement(req, top=100000)
json.dump(r, open(out, "w"), ensure_ascii=False)
print(r["total_candidates"], r.get("llm_budget"))
