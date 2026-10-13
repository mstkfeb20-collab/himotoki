#!/usr/bin/env bash
# Run every prepared task under both conditions with identical flags.
#   MODEL=opus BUDGET=10 ./run.sh ~/compare
# Only differences between A and B: the MCP config (none / himotoki) and one line in the
# prompt telling B the himotoki tools exist. Re-running skips results already written.
set -u
OUT=${1:?usage: run.sh <folder made by prepare.py>}
MODEL=${MODEL:-opus}
BUDGET=${BUDGET:-10}
# with an API key, bare mode skips CLAUDE.md, hooks, skills, plugins and auto memory, so
# nothing from this machine's setup leaks into either condition (BARE=0 to turn off)
EXTRA=()
if [ -n "${ANTHROPIC_API_KEY:-}" ] && [ "${BARE:-1}" != 0 ]; then EXTRA=(--bare); fi
HERE=$(cd "$(dirname "$0")" && pwd)
SCHEMA=$(cat "$HERE/schema.json")
HINT='影響調査には himotoki の MCP ツールを使えます。最初に impact_files で候補のファイルと、そこに至る理由を受け取り、それを手がかりに中身を確かめてください（get_element・neighbors で要素の中身とつながりを見られます）。候補は確定ではないので、必要なら grep などで補ってください。'
# read-only investigation: file reading and searching only, no edits, no web
ALLOW=(Read Grep Glob "Bash(grep *)" "Bash(rg *)" "Bash(find *)" "Bash(ls *)" "Bash(cat *)" "Bash(head *)"
       "Bash(tail *)" "Bash(wc *)" "Bash(sed -n *)" "Bash(git grep *)" "Bash(git ls-files *)" "mcp__himotoki")
# ALLOW_PYTHON=1 lets both conditions run python3 (e.g. to read Excel / Word design documents)
[ "${ALLOW_PYTHON:-0}" = 1 ] && ALLOW+=("Bash(python3 *)" "Bash(python *)")
DENY=(WebFetch WebSearch Edit Write NotebookEdit)
n=0
for dir in "$OUT"/*/; do
  dir=${dir%/}
  id=$(basename "$dir")
  [ -f "$dir/prompt.txt" ] || continue
  order="A B"; [ $((n % 2)) -eq 1 ] && order="B A"      # alternate who goes first
  n=$((n + 1))
  for c in $order; do
    res="$dir/result-$c.json"
    # skip only a run that finished with an answer; failed or capped runs are run again
    # (FORCE=1 runs everything again; the previous result is kept as result-X.prev.json)
    if [ -s "$res" ] && [ "${FORCE:-0}" != 1 ]; then
      st=$(python3 - "$res" <<'PY'
import json, sys
try:
    r = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print(f"unreadable ({type(e).__name__})"); sys.exit()
ok = not r.get("is_error") and r.get("subtype") in (None, "success") and (r.get("structured_output") or {}).get("files") is not None
print("ok" if ok else f"failed: {r.get('subtype')} {str(r.get('result') or '')[:120]}")
PY
)
      if [ "$st" = ok ]; then echo "$id $c: done already"; continue; fi
      echo "$id $c: previous run $st -> running again"
    fi
    [ -s "$res" ] && mv "$res" "${res%.json}.prev.json"
    p=$(cat "$dir/prompt.txt")
    [ "$c" = B ] && p="$p"$'\n\n'"$HINT"
    start=$(date +%s)
    # stream-json keeps every tool call, so the score can say how often each tool (himotoki,
    # grep, file reads, subagents) was used; the last line is the same result object as json
    ( cd "$dir/$c" && claude ${EXTRA[@]+"${EXTRA[@]}"} -p "$p" --model "$MODEL" --output-format stream-json --verbose \
        --json-schema "$SCHEMA" \
        --strict-mcp-config --mcp-config "$dir/mcp-$c.json" \
        --permission-mode dontAsk --allowedTools "${ALLOW[@]}" --disallowedTools "${DENY[@]}" \
        --no-session-persistence --max-budget-usd "$BUDGET" ) > "$dir/stream-$c.jsonl" 2> "$dir/stderr-$c.log"
    python3 - "$dir/stream-$c.jsonl" "$res" <<'PY'
import collections, json, sys
tools, result = collections.Counter(), None
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        m = json.loads(line)
    except ValueError:
        continue
    if m.get("type") == "assistant":
        for b in (m.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tools[b.get("name", "?")] += 1
    elif m.get("type") == "result":
        result = m
result = result or {"is_error": True, "subtype": "no_result", "result": "no result line in the stream"}
result["tool_calls"] = dict(tools)
json.dump(result, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
PY
    echo "$id $c: $(( $(date +%s) - start ))s"
  done
done
