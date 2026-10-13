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
HINT='影響調査には himotoki の MCP ツール（analyze_requirement・search_elements・get_element・neighbors）を使えます。'
# read-only investigation: file reading and searching only, no edits, no web
ALLOW=(Read Grep Glob "Bash(grep *)" "Bash(rg *)" "Bash(find *)" "Bash(ls *)" "Bash(cat *)" "Bash(head *)"
       "Bash(tail *)" "Bash(wc *)" "Bash(sed -n *)" "Bash(git grep *)" "Bash(git ls-files *)" "mcp__himotoki")
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
    [ -s "$res" ] && { echo "$id $c: done already"; continue; }
    p=$(cat "$dir/prompt.txt")
    [ "$c" = B ] && p="$p"$'\n\n'"$HINT"
    start=$(date +%s)
    ( cd "$dir/$c" && claude ${EXTRA[@]+"${EXTRA[@]}"} -p "$p" --model "$MODEL" --output-format json --json-schema "$SCHEMA" \
        --strict-mcp-config --mcp-config "$dir/mcp-$c.json" \
        --permission-mode dontAsk --allowedTools "${ALLOW[@]}" --disallowedTools "${DENY[@]}" \
        --no-session-persistence --max-budget-usd "$BUDGET" ) > "$res" 2> "$dir/stderr-$c.log"
    echo "$id $c: $(( $(date +%s) - start ))s"
  done
done
