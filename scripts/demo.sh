#!/usr/bin/env bash
# demo.sh - run the demonstration scenario end to end.
#
#   1. shows the problem: plain pytest gives different results run to run
#   2. runs detect.sh on scenario/ (condition matrix -> results.csv ->
#      analysis and report stages)
#   3. prints pass counts per condition next to the expected category from
#      the answer key in docs/scenario.md
#
# Extra arguments are passed to detect.sh, e.g. scripts/demo.sh --repeats 30
set -euo pipefail

TOOL_ROOT=$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
SCENARIO=$TOOL_ROOT/scenario
ANSWER_KEY=$TOOL_ROOT/docs/scenario.md
OUT_DIR=$TOOL_ROOT/out
PYTHON=""

if [[ -x $TOOL_ROOT/.venv/bin/python ]]; then
    PYTHON=$TOOL_ROOT/.venv/bin/python
else
    PYTHON=$(command -v python3 || true)
fi

die() {
    echo "demo.sh: error: $1" >&2
    exit "${2:-1}"
}

heading() {
    printf '\n== %s\n' "$1"
}

args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    case ${args[i]} in
        -h|--help)
            exec "$TOOL_ROOT/scripts/detect.sh" --help
            ;;
        --out)
            if [[ $((i + 1)) -lt ${#args[@]} ]]; then
                OUT_DIR=${args[i + 1]}
            fi
            ;;
        --python)
            if [[ $((i + 1)) -lt ${#args[@]} ]]; then
                PYTHON=${args[i + 1]}
            fi
            ;;
    esac
done

[[ -n $PYTHON && -x $PYTHON ]] || die "Python interpreter not found; run the setup commands in README.md first" 3
[[ -d $SCENARIO ]] || die "scenario folder not found at $SCENARIO" 2
[[ -f $ANSWER_KEY ]] || die "answer key not found at $ANSWER_KEY" 2

# ------------------------------------------------- 1. the manual baseline

heading "The problem: the same suite, no code changes, run twice with plain pytest"
for attempt in 1 2; do
    summary=$(cd "$SCENARIO" && "$PYTHON" -m pytest -q -p no:randomly -p no:cacheprovider < /dev/null 2>&1 | tail -n 1 || true)
    echo "  run $attempt: $summary"
done
echo "  Which failures are real bugs, which are flaky, and why? Plain pytest cannot say."

# --------------------------------------------------- 2. the condition matrix

heading "Running scripts/detect.sh on scenario/"
detect_rc=0
"$TOOL_ROOT/scripts/detect.sh" --project "$SCENARIO" --out "$OUT_DIR" "$@" || detect_rc=$?
[[ $detect_rc -eq 0 ]] || die "detect.sh failed with exit code $detect_rc" "$detect_rc"
OUT_DIR=$(cd -P "$OUT_DIR" && pwd -P)
if [[ $OUT_DIR == "$TOOL_ROOT"/* ]]; then
    OUT_REL=${OUT_DIR#$TOOL_ROOT/}
else
    OUT_REL=$OUT_DIR
fi

# ---------------------------------------- 3. pass counts vs the answer key

heading "Passes per condition (from $OUT_REL/results.csv) vs the answer key (docs/scenario.md)"
awk -F',' '
    FNR == NR {
        if ($0 ~ /^\| `test_/) {
            n = split($0, part, "`")
            expected[part[2]] = part[n - 1]
        }
        next
    }
    FNR == 1 { next }
    {
        line = $0
        outcome = $NF
        sub(/,[^,]*$/, "", line)
        sub(/^[^,]*,[^,]*,("[^"]*"|[^,]*),[^,]*,/, "", line)
        gsub(/^"|"$/, "", line)
        gsub(/""/, "\"", line)
        name = line
        sub(/.*::/, "", name)
        condition = $2
        if (!(name in seen)) { seen[name] = 1; order[++n_tests] = name }
        total[name, condition]++
        if (outcome == "passed") passed[name, condition]++
    }
    END {
        printf "  %-46s %-15s %9s %9s %9s %9s\n", "test", "expected", "repeat", "hashseed", "order", "isolated"
        split("repeat hashseed order isolated", cond, " ")
        for (i = 1; i <= n_tests; i++) {
            name = order[i]
            want = (name in expected) ? expected[name] : "(not in key)"
            printf "  %-46s %-15s", name, want
            for (c = 1; c <= 4; c++) {
                key = name SUBSEP cond[c]
                cell = (key in total) ? (passed[key] + 0) "/" total[key] : "-"
                printf " %9s", cell
            }
            printf "\n"
        }
    }
' "$ANSWER_KEY" "$OUT_DIR/results.csv"

# ----------------------------------------------------------- 4. where it is

raw_lines=$(cat "$OUT_DIR"/logs/run_*.log 2>/dev/null | wc -l | tr -d ' ')
rows=$(($(wc -l < "$OUT_DIR/results.csv") - 1))
heading "Outputs"
echo "  raw pytest logs:  $OUT_REL/logs/ ($raw_lines lines)"
echo "  structured data:  $OUT_REL/results.csv ($rows rows), $OUT_REL/run_meta.csv"
if [[ -f $OUT_DIR/diagnosis.json ]]; then
    echo "  diagnosis:        $OUT_REL/diagnosis.json"
else
    echo "  diagnosis:        not produced (analysis stage not in this checkout)"
fi
if [[ -f $OUT_DIR/report.md ]]; then
    echo "  triage report:    $OUT_REL/report.md"
else
    echo "  triage report:    not produced (report stage not in this checkout)"
fi
