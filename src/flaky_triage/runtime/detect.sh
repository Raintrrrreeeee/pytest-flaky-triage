#!/usr/bin/env bash
# detect.sh - run a pytest project many times under a condition matrix and
# record every test's outcome per run in a CSV for the analysis stage.
#
# Each condition changes exactly one factor and holds the others fixed:
#   repeat    same order, PYTHONHASHSEED=0, N runs      -> randomness, races
#   hashseed  same order, one run per hash seed         -> set/hash-order dependence
#   order     reversed order + K seeded shuffles        -> order / shared state
#   tz        same order, PYTHONHASHSEED=0, one per TZ  -> timezone / local-time dependence
#   isolated  every test alone in its own process       -> shared state
#
# Compatible with Bash 3.2+ (macOS default) and Bash 5+ (Linux).

set -euo pipefail

SCRIPT_DIR=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
if [[ ${FLAKY_INSTALLED_PACKAGE:-0} != 1 && -d $SCRIPT_DIR/../../../src/flaky_triage && -d $SCRIPT_DIR/../../../scripts ]]; then
    TOOL_ROOT=$(cd -P "$SCRIPT_DIR/../../.." && pwd -P)
else
    TOOL_ROOT=$(cd -P "$SCRIPT_DIR/.." && pwd -P)
fi
if [[ -d $SCRIPT_DIR/lib ]]; then
    LIB_DIR="$SCRIPT_DIR/lib"
else
    LIB_DIR="$TOOL_ROOT/scripts/lib"
fi

# Exit codes (documented in README.md)
readonly E_USAGE=2      # bad arguments
readonly E_ENV=3        # Python or pytest missing
readonly E_COLLECT=4    # pytest could not collect the project, or found no tests
readonly E_RUNS=5       # no run produced parseable results
readonly E_REPORT=6     # analysis / report stage failed

PROJECT=""
REPEATS=10
HASH_SEEDS="1 2 3 4 5 6 7 8"
ORDERS=5
TIMEZONES="UTC Pacific/Kiritimati America/Pago_Pago"
ISOLATED=1
ISOLATED_SCOPE="all"
TIMEOUT=60
if [[ ${FLAKY_INSTALLED_PACKAGE:-0} == 1 ]]; then
    OUT_DIR="$(pwd -P)/out"
else
    OUT_DIR="$TOOL_ROOT/out"
fi
PYTHON=""
TARGET_SUBPATH=""
KEYWORD_EXPR=""
REPORT=yes
REPORT_ONLY=no
REPORT_LANG="en"
CLI_USAGE_NAME=${FLAKY_CLI_NAME:-scripts/detect.sh}
CLI_ERR_NAME=${FLAKY_CLI_NAME:-detect.sh}

usage() {
    cat <<EOF
Usage: $CLI_USAGE_NAME --project PATH [options]

Runs the pytest suite in PATH under several conditions and writes
per-test outcomes to OUT/results.csv.

Options:
  --project PATH             pytest project to investigate (required)
  --target SUBPATH           optional test file or directory inside project
  -k EXPR                    only run tests matching pytest keyword expression
  --repeats N                plain repeated runs, N >= 1 (default $REPEATS)
  --hash-seeds "S..."        PYTHONHASHSEED values, one run each; "" to skip
                             (default "$HASH_SEEDS")
  --orders K                 seeded shuffled-order runs, plus one reversed run;
                             0 to skip (default $ORDERS)
  --timezones "TZ..."        TZ environment values, one run each; "" to skip
                             (default "$TIMEZONES")
  --isolated N               runs of each test on its own; 0 to skip (default $ISOLATED)
  --isolated-scope MODE      'all' (every test) or 'smart' (only tests with
                             suite failures) (default $ISOLATED_SCOPE)
  --timeout SECONDS          limit for a single pytest run (default $TIMEOUT)
  --out DIR                  output folder (default out/)
  --python PATH              Python interpreter with pytest installed for target
  --lang LANG                report language: 'en' or 'zh' (default $REPORT_LANG)
  --report-only              regenerate report.md from existing output without
                             re-running pytest
  --no-report                stop after writing results.csv
  -h, --help                 show this help
EOF
}

die() {
    [[ -t 2 ]] && printf '\r\033[K' >&2
    echo "$CLI_ERR_NAME: error: $1" >&2
    exit "${2:-$E_USAGE}"
}

say() {
    [[ -t 2 ]] && printf '\r\033[K' >&2
    echo "[detect] $*" >&2
}

is_uint() {
    [[ $1 =~ ^[0-9]{1,9}$ ]]
}

is_tz() {
    [[ $1 =~ ^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$ ]]
}

# ---------------------------------------------------------------- arguments

ORIG_ARGS=("$@")
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --no-report) REPORT=no; shift; continue ;;
        --report-only) REPORT_ONLY=yes; shift; continue ;;
        --*=*)
            opt_name="${1%%=*}"
            opt_val="${1#*=}"
            shift
            set -- "$opt_name" "$opt_val" "$@"
            continue
            ;;
        --project|--target|-k|--repeats|--hash-seeds|--orders|--timezones|--isolated|--isolated-scope|--timeout|--out|--python|--lang)
            [[ $# -ge 2 ]] || die "$1 needs a value (see --help)"
            ;;
        *) die "unknown option '$1' (see --help)" ;;
    esac
    case $1 in
        --project)        PROJECT=$2 ;;
        --target)         TARGET_SUBPATH=$2 ;;
        -k)               KEYWORD_EXPR=$2 ;;
        --repeats)        REPEATS=$2 ;;
        --hash-seeds)     HASH_SEEDS=$2 ;;
        --orders)         ORDERS=$2 ;;
        --timezones)      TIMEZONES=$2 ;;
        --isolated)       ISOLATED=$2 ;;
        --isolated-scope) ISOLATED_SCOPE=$2 ;;
        --timeout)        TIMEOUT=$2 ;;
        --out)            OUT_DIR=$2 ;;
        --python)         PYTHON=$2 ;;
        --lang)           REPORT_LANG=$2 ;;
    esac
    shift 2
done

[[ -n $PROJECT ]] || { usage >&2; die "--project is required"; }
[[ -d $PROJECT ]] || die "project folder '$PROJECT' does not exist"
PROJECT=$(cd -P "$PROJECT" && pwd -P)

is_uint "$REPEATS" || die "--repeats must be a whole number >= 1 (got '$REPEATS')"
is_uint "$ORDERS" || die "--orders must be a whole number >= 0 (got '$ORDERS')"
is_uint "$ISOLATED" || die "--isolated must be a whole number >= 0 (got '$ISOLATED')"
is_uint "$TIMEOUT" || die "--timeout must be a whole number of seconds >= 1 (got '$TIMEOUT')"
case $ISOLATED_SCOPE in
    all|smart) ;;
    *) die "--isolated-scope must be 'all' or 'smart' (got '$ISOLATED_SCOPE')" ;;
esac
case $REPORT_LANG in
    en|zh) ;;
    *) die "--lang must be 'en' or 'zh' (got '$REPORT_LANG')" ;;
esac
if [[ $REPORT == no && $REPORT_ONLY == yes ]]; then
    die "--no-report and --report-only cannot be used together"
fi

REPEATS=$((10#$REPEATS)); ORDERS=$((10#$ORDERS))
ISOLATED=$((10#$ISOLATED)); TIMEOUT=$((10#$TIMEOUT))
[[ $REPEATS -ge 1 ]] || die "--repeats must be a whole number >= 1 (got '$REPEATS')"
[[ $TIMEOUT -ge 1 ]] || die "--timeout must be a whole number of seconds >= 1 (got '$TIMEOUT')"

SEED_ARR=()
set -f
for seed in $HASH_SEEDS; do
    is_uint "$seed" || { set +f; die "--hash-seeds values must be whole numbers 0..999999999 (got '$seed')"; }
    SEED_ARR+=("$((10#$seed))")
done
set +f
HASH_SEEDS="${SEED_ARR[*]:-}"

TZ_ARR=()
set -f
for tz_val in $TIMEZONES; do
    is_tz "$tz_val" || { set +f; die "--timezones values must be valid timezone identifiers (got '$tz_val')"; }
    TZ_ARR+=("$tz_val")
done
set +f
TIMEZONES="${TZ_ARR[*]:-}"

# Reject symlinked output paths before creating directories so symlink targets
# cannot be modified or cleaned.
if [[ -L $OUT_DIR ]]; then
    die "--out must not be a symlink (got '$OUT_DIR')"
fi
mkdir -p "$OUT_DIR" || die "cannot create output folder '$OUT_DIR'"
OUT_DIR=$(cd -P "$OUT_DIR" && pwd -P)

[[ $OUT_DIR != "/" && $OUT_DIR != "${HOME:-/}" ]] || die "--out must not be / or HOME (got '$OUT_DIR')"
[[ $OUT_DIR != "$TOOL_ROOT" ]] || die "--out must be a sub-folder or dedicated directory, not $TOOL_ROOT"
[[ $OUT_DIR != "$PROJECT" ]] || die "--out must not be the project folder"
case $PROJECT/ in
    "$OUT_DIR"/*) die "--out must not contain the project folder" ;;
esac
case $TOOL_ROOT/ in
    "$OUT_DIR"/*) die "--out must not contain the tool folder" ;;
esac
if [[ $OUT_DIR == "$TOOL_ROOT"/* ]]; then
    OUT_REL=${OUT_DIR#$TOOL_ROOT/}
else
    OUT_REL=$OUT_DIR
fi

# Resolve TOOL_PYTHON (runs internal helpers, analysis, and report stages)
TOOL_PYTHON=""
if [[ -n ${FLAKY_TOOL_PYTHON:-} && -x ${FLAKY_TOOL_PYTHON:-} ]]; then
    TOOL_PYTHON=$FLAKY_TOOL_PYTHON
elif [[ -x $TOOL_ROOT/.venv/bin/python ]]; then
    TOOL_PYTHON=$TOOL_ROOT/.venv/bin/python
elif [[ -x $TOOL_ROOT/.venv/Scripts/python.exe ]]; then
    TOOL_PYTHON=$TOOL_ROOT/.venv/Scripts/python.exe
else
    TOOL_PYTHON=$(command -v python3 || command -v python || true)
fi
[[ -n $TOOL_PYTHON && -x $TOOL_PYTHON ]] || die "no Python interpreter found for tool; create .venv as described in README.md" "$E_ENV"

if [[ ${OSTYPE:-} == msys* || ${OSTYPE:-} == cygwin* || ${OS:-} == Windows_NT ]]; then
    FLAKY_PURE_PYTHON=1 FLAKY_CLI_NAME="${FLAKY_CLI_NAME:-scripts/detect.sh}" \
        PYTHONPATH="$TOOL_ROOT/src${PYTHONPATH:+;$PYTHONPATH}" \
        exec "$TOOL_PYTHON" -m flaky_triage.cli ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
fi

TOOL_PYTHONPATH="$TOOL_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

run_analysis_and_report() {
    if [[ $REPORT_ONLY == yes && ! -f $OUT_DIR/diagnosis.json && ! -f $OUT_DIR/results.csv ]]; then
        die "--report-only requires an existing $OUT_REL/diagnosis.json or $OUT_REL/results.csv" "$E_REPORT"
    fi
    if [[ $REPORT_ONLY != yes || ! -f $OUT_DIR/diagnosis.json ]]; then
        if [[ ! -d $TOOL_ROOT/src/analysis && ${FLAKY_INSTALLED_PACKAGE:-0} != 1 ]]; then
            say "analysis stage not in the repository yet; stopping after results.csv"
            exit 0
        fi
        local analysis_rc=0
        PYTHONPATH="$TOOL_PYTHONPATH" "$TOOL_PYTHON" -m analysis "$OUT_DIR/results.csv" "$OUT_DIR" || analysis_rc=$?
        case $analysis_rc in
            0) say "diagnosis: $OUT_REL/diagnosis.json" ;;
            2) die "analysis stage rejected its arguments (exit 2)" "$E_REPORT" ;;
            3) die "analysis stage could not read results.csv or run_meta.csv (exit 3)" "$E_REPORT" ;;
            4) die "analysis stage found invalid or inconsistent data in results.csv (exit 4)" "$E_REPORT" ;;
            5) die "analysis stage could not write diagnosis.json (exit 5)" "$E_REPORT" ;;
            *) die "analysis stage failed with exit code $analysis_rc" "$E_REPORT" ;;
        esac
    fi

    if [[ ! -d $TOOL_ROOT/src/report && ${FLAKY_INSTALLED_PACKAGE:-0} != 1 ]]; then
        say "report stage not in the repository yet; stopping after diagnosis.json"
        exit 0
    fi
    local report_rc=0
    PYTHONPATH="$TOOL_PYTHONPATH" "$TOOL_PYTHON" -m report "$OUT_DIR/diagnosis.json" "$OUT_DIR" --project "$PROJECT" --lang "$REPORT_LANG" || report_rc=$?
    case $report_rc in
        0) say "report:    $OUT_REL/report.md" ;;
        2) die "report stage rejected its arguments (exit 2)" "$E_REPORT" ;;
        3) die "report stage could not read diagnosis.json (exit 3)" "$E_REPORT" ;;
        4) die "report stage found invalid or inconsistent data in diagnosis.json (exit 4)" "$E_REPORT" ;;
        5) die "report stage could not write report.md (exit 5)" "$E_REPORT" ;;
        *) die "report stage failed with exit code $report_rc" "$E_REPORT" ;;
    esac
}

if [[ $REPORT_ONLY == yes ]]; then
    run_analysis_and_report
    exit 0
fi

# Resolve TARGET_PYTHON (runs pytest on the target project)
TARGET_PYTHON=""
if [[ -n $PYTHON ]]; then
    TARGET_PYTHON=$PYTHON
else
    for candidate in \
        "$PROJECT/.venv/bin/python" \
        "$PROJECT/venv/bin/python" \
        "${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}" \
        "$TOOL_PYTHON"; do
        if [[ -n $candidate && -x $candidate ]] && "$candidate" -m pytest --version >/dev/null 2>&1; then
            TARGET_PYTHON=$candidate
            break
        fi
    done
    [[ -n $TARGET_PYTHON ]] || TARGET_PYTHON=$TOOL_PYTHON
fi
[[ -n $TARGET_PYTHON && -x $TARGET_PYTHON ]] || die "no Python interpreter found; create .venv as described in README.md" "$E_ENV"
"$TARGET_PYTHON" -m pytest --version >/dev/null 2>&1 \
    || die "pytest is not installed for $TARGET_PYTHON; run: pip install -r requirements.txt" "$E_ENV"

TARGET_PYTHONPATH="$LIB_DIR"
if [[ -d $PROJECT/src ]]; then
    TARGET_PYTHONPATH="$TARGET_PYTHONPATH:$PROJECT/src"
fi
TARGET_PYTHONPATH="$TARGET_PYTHONPATH:$PROJECT${PYTHONPATH:+:$PYTHONPATH}"

# Clean up temporary per-run folders and exit immediately on Ctrl+C / SIGTERM
CURRENT_RUN_TMP=""
cleanup_on_signal() {
    local code=$1
    [[ -n $CURRENT_RUN_TMP && -d $CURRENT_RUN_TMP ]] && rm -rf "$CURRENT_RUN_TMP"
    rm -f "$OUT_DIR/orders/single.txt" "$OUT_DIR/orders/smart_isolated.txt" "$OUT_DIR/collected.tmp"
    echo "" >&2
    say "aborted by signal (exit $code)"
    exit "$code"
}
trap 'cleanup_on_signal 130' INT
trap 'cleanup_on_signal 143' TERM

# ------------------------------------------------------------------ helpers

run_with_timeout() {
    local secs=$1
    shift
    local rc=0
    "$TOOL_PYTHON" "$LIB_DIR/flaky_plugin.py" run-timeout "$secs" "$@" || rc=$?
    return "$rc"
}

# Fixed pytest options to neutralize conflicting plugins while preserving custom plugins:
PYTEST_OPTS=(
    --rootdir="$PROJECT"
    -o console_output_style=progress
    -p flaky_plugin
    -p no:randomly
    -p no:random_order
    -p no:order
    -p no:ordering
    -p no:reverse
    -p no:cacheprovider
    -p no:sugar
    -p no:xdist
    -p no:cov
    -p no:flaky
    -p no:rerunfailures
    -p no:flakefinder
    -p no:instafail
    --maxfail=0
    --tb=short
    --verbosity=1
    --color=no
    --capture=fd
)
if [[ $OUT_DIR == "$PROJECT"/* ]]; then
    PYTEST_OPTS+=(--ignore="$OUT_DIR")
fi

EXTRA_COLLECT_ARGS=()
[[ -n $KEYWORD_EXPR ]] && EXTRA_COLLECT_ARGS+=(-k "$KEYWORD_EXPR")
[[ -n $TARGET_SUBPATH ]] && EXTRA_COLLECT_ARGS+=("$TARGET_SUBPATH")

shuffle_ids() {
    "$TOOL_PYTHON" "$LIB_DIR/flaky_plugin.py" shuffle "$1" "$2" "$3"
}

reverse_ids() {
    "$TOOL_PYTHON" "$LIB_DIR/flaky_plugin.py" reverse "$1" "$2"
}

RUN_ID=0
TOTAL_TIMEOUTS=0
TOTAL_CRASHES=0
TOTAL_PARSED=0

# run_one CONDITION VALUE HASH_SEED EXPECTED_FILE [ORDER_FILE] [pytest target args...]
run_one() {
    local condition=$1 value=$2 hash_seed=$3 expected=$4 order_file=${5:-}
    shift $(( $# >= 5 ? 5 : 4 ))
    RUN_ID=$((RUN_ID + 1))
    if [[ -t 2 ]]; then
        printf '\r[detect] running %d/%d (%s %s)...\033[K' "$RUN_ID" "${N_RUNS:-0}" "$condition" "$value" >&2
    fi
    local log_rel
    if [[ ${N_RUNS:-0} -ge 1000 ]]; then
        log_rel=logs/run_$(printf '%05d' "$RUN_ID").log
    else
        log_rel=logs/run_$(printf '%03d' "$RUN_ID").log
    fi
    local log_file=$OUT_DIR/$log_rel

    CURRENT_RUN_TMP="$OUT_DIR/.tmp_run_$RUN_ID"
    mkdir -p "$CURRENT_RUN_TMP"
    local events_file="$CURRENT_RUN_TMP/events.jsonl"

    local rc=0
    (
        cd "$PROJECT"
        unset PYTEST_ADDOPTS
        export PYTHONHASHSEED=$hash_seed
        if [[ $condition == tz ]]; then
            export TZ="$value"
        else
            export TZ="UTC"
        fi
        export PYTHONDONTWRITEBYTECODE=1
        export PYTHONPATH="$TARGET_PYTHONPATH"
        export TMPDIR="$CURRENT_RUN_TMP"
        export FLAKY_EVENTS_FILE="$events_file"
        if [[ -n $order_file ]]; then
            export FLAKY_ORDER_FILE="$order_file"
        else
            unset FLAKY_ORDER_FILE
        fi
        unset FLAKY_COLLECT_FILE
        run_with_timeout "$TIMEOUT" "$TARGET_PYTHON" -m pytest "${PYTEST_OPTS[@]}" ${1+"$@"}
    ) > "$log_file" 2>&1 || rc=$?

    if [[ $rc -eq 130 || $rc -eq 143 ]]; then
        cleanup_on_signal "$rc"
    fi

    local status timed_out=0
    case $rc in
        0|1) status=ok ;;
        124) status=timeout; timed_out=1; TOTAL_TIMEOUTS=$((TOTAL_TIMEOUTS + 1))
             say "warning: run $RUN_ID ($condition $value) hit the ${TIMEOUT}s timeout" ;;
        2)   status=interrupted ;;
        3)   status=internal_error ;;
        4)   status=usage_error ;;
        5)   status=no_tests ;;
        *)   status=crashed ;;
    esac
    if [[ $status != ok && $status != timeout ]]; then
        TOTAL_CRASHES=$((TOTAL_CRASHES + 1))
        say "warning: run $RUN_ID ($condition $value) ended with pytest exit code $rc ($status); see $OUT_REL/$log_rel"
    fi

    local rows=""
    if ! rows=$("$TOOL_PYTHON" "$LIB_DIR/flaky_plugin.py" events-to-csv \
                "$RUN_ID" "$condition" "$value" "$timed_out" "$expected" "$events_file" 2>/dev/null); then
        rows=$(awk -v run_id="$RUN_ID" -v condition="$condition" -v cvalue="$value" \
                   -v timed_out="$timed_out" -f "$LIB_DIR/parse_log.awk" "$expected" "$log_file")
    fi
    rm -rf "$CURRENT_RUN_TMP"
    CURRENT_RUN_TMP=""

    if [[ -n $rows ]]; then
        printf '%s\n' "$rows" >> "$OUT_DIR/results.csv"
    fi

    local n_expected n_parsed
    n_expected=$(grep -c . "$expected" || true)
    if [[ -z $rows ]]; then
        n_parsed=0
    else
        n_parsed=$(printf '%s\n' "$rows" | grep -cvE ',(not_run|timeout)$' || true)
    fi
    TOTAL_PARSED=$((TOTAL_PARSED + n_parsed))
    if [[ $status == ok && $n_parsed -lt $n_expected ]]; then
        TOTAL_CRASHES=$((TOTAL_CRASHES + 1))
        say "warning: run $RUN_ID ($condition $value) reported only $n_parsed of $n_expected tests; see $OUT_REL/$log_rel"
    fi

    local seconds
    seconds=$(sed -n 's/^=* .* in \([0-9][0-9]*\.[0-9][0-9]*\)s.*=*$/\1/p' "$log_file" | tail -n 1)

    printf '%s,%s,"%s",%s,%s,%s,%s,%s,%s,%s\n' \
        "$RUN_ID" "$condition" "${value//\"/\"\"}" "$hash_seed" "$rc" "$status" \
        "$seconds" "$n_expected" "$n_parsed" "$log_rel" >> "$OUT_DIR/run_meta.csv"
}

# --------------------------------------------------------------- collection

say "project: $PROJECT"
say "python:  $TARGET_PYTHON ($("$TARGET_PYTHON" -m pytest --version 2>&1 | head -n 1))"

COLLECT_LOG=$OUT_DIR/collect.log
COLLECT_TMP=$OUT_DIR/collected.tmp
rm -f "$COLLECT_TMP"
collect_rc=0
(
    cd "$PROJECT"
    unset PYTEST_ADDOPTS
    export PYTHONHASHSEED=0
    export TZ=UTC
    export PYTHONDONTWRITEBYTECODE=1
    export PYTHONPATH="$TARGET_PYTHONPATH"
    export FLAKY_COLLECT_FILE="$COLLECT_TMP"
    unset FLAKY_ORDER_FILE FLAKY_EVENTS_FILE
    run_with_timeout "$TIMEOUT" "$TARGET_PYTHON" -m pytest \
        "${PYTEST_OPTS[@]}" --collect-only --verbosity=-1 \
        ${EXTRA_COLLECT_ARGS[@]+"${EXTRA_COLLECT_ARGS[@]}"}
) > "$COLLECT_LOG" 2>&1 || collect_rc=$?

case $collect_rc in
    0) ;;
    5) rm -f "$COLLECT_TMP"
       if [[ -d $PROJECT/scenario/tests ]]; then
           say "hint: found benchmark suite at '$PROJECT/scenario' — try: --project $PROJECT/scenario"
       fi
       die "pytest found no tests in $PROJECT" "$E_COLLECT" ;;
    124) rm -f "$COLLECT_TMP"; die "pytest collection timed out after ${TIMEOUT}s in $PROJECT" "$E_COLLECT" ;;
    *) rm -f "$COLLECT_TMP"
       tail -n 15 "$COLLECT_LOG" >&2
       if [[ -d $PROJECT/scenario/tests ]]; then
           say "hint: found benchmark suite at '$PROJECT/scenario' — try: --project $PROJECT/scenario"
       fi
       die "pytest could not collect the project (exit code $collect_rc); full output in $OUT_REL/collect.log" "$E_COLLECT" ;;
esac

if [[ ! -s $COLLECT_TMP ]]; then
    awk '/^=+ / { next } /^$/ { if (found) exit; next } /::/ { sub(/\r$/, ""); found=1; print }' "$COLLECT_LOG" > "$COLLECT_TMP"
fi
N_TESTS=$(grep -c . "$COLLECT_TMP" || true)
[[ $N_TESTS -gt 0 ]] || { rm -f "$COLLECT_TMP"; die "could not read any test IDs from $OUT_REL/collect.log" "$E_COLLECT"; }

rm -rf "$OUT_DIR/logs" "$OUT_DIR/orders"
rm -f "$OUT_DIR/results.csv" "$OUT_DIR/run_meta.csv" "$OUT_DIR/diagnosis.json" "$OUT_DIR/report.md"
mkdir -p "$OUT_DIR/logs" "$OUT_DIR/orders"
mv "$COLLECT_TMP" "$OUT_DIR/collected.txt"

N_SEEDS=${#SEED_ARR[@]}
N_TZS=${#TZ_ARR[@]}
N_ORDER_RUNS=0
[[ $ORDERS -gt 0 ]] && N_ORDER_RUNS=$((ORDERS + 1))
BASE_RUNS=$((REPEATS + N_SEEDS + N_ORDER_RUNS + N_TZS))
N_RUNS=$((BASE_RUNS + N_TESTS * ISOLATED))
if [[ $ISOLATED -gt 0 && $ISOLATED_SCOPE == smart ]]; then
    say "collected $N_TESTS tests; planned up to $N_RUNS runs ($BASE_RUNS suite runs + smart isolated for failing tests; timeout ${TIMEOUT}s each)"
else
    say "collected $N_TESTS tests; planned $N_RUNS runs (timeout ${TIMEOUT}s each)"
fi

echo 'run_id,condition,condition_value,position,test_id,outcome' > "$OUT_DIR/results.csv"
echo 'run_id,condition,condition_value,hash_seed,exit_code,status,seconds,tests_expected,tests_reported,log_path' > "$OUT_DIR/run_meta.csv"

# ----------------------------------------------------------- condition matrix

START=$SECONDS
COLLECTED=$OUT_DIR/collected.txt

for ((r = 1; r <= REPEATS; r++)); do
    run_one repeat "$r" 0 "$COLLECTED" "$COLLECTED" ${EXTRA_COLLECT_ARGS[@]+"${EXTRA_COLLECT_ARGS[@]}"}
done
say "repeat:   $REPEATS runs done"

for ((s_idx = 0; s_idx < N_SEEDS; s_idx++)); do
    seed=${SEED_ARR[$s_idx]}
    run_one hashseed "$seed" "$seed" "$COLLECTED" "$COLLECTED" ${EXTRA_COLLECT_ARGS[@]+"${EXTRA_COLLECT_ARGS[@]}"}
done
say "hashseed: $N_SEEDS runs done"

if [[ $ORDERS -gt 0 ]]; then
    order_file=$OUT_DIR/orders/reverse.txt
    reverse_ids "$COLLECTED" "$order_file"
    run_one order reverse 0 "$order_file" "$order_file" ${EXTRA_COLLECT_ARGS[@]+"${EXTRA_COLLECT_ARGS[@]}"}
    for ((k = 1; k <= ORDERS; k++)); do
        order_file=$OUT_DIR/orders/shuffle_$k.txt
        shuffle_ids "$COLLECTED" "$k" "$order_file"
        run_one order "shuffle:$k" 0 "$order_file" "$order_file" ${EXTRA_COLLECT_ARGS[@]+"${EXTRA_COLLECT_ARGS[@]}"}
    done
    say "order:    $N_ORDER_RUNS runs done"
fi

if [[ $N_TZS -gt 0 ]]; then
    for ((tz_idx = 0; tz_idx < N_TZS; tz_idx++)); do
        tz_val=${TZ_ARR[$tz_idx]}
        run_one tz "$tz_val" 0 "$COLLECTED" "$COLLECTED" ${EXTRA_COLLECT_ARGS[@]+"${EXTRA_COLLECT_ARGS[@]}"}
    done
    say "tz:       $N_TZS runs done"
fi

if [[ $ISOLATED -gt 0 ]]; then
    single=$OUT_DIR/orders/single.txt
    iso_candidates=$COLLECTED
    if [[ $ISOLATED_SCOPE == smart ]]; then
        iso_candidates=$OUT_DIR/orders/smart_isolated.txt
        awk -F',' '
            NR == 1 { next }
            $2 != "tz" && $NF != "passed" && $NF != "skipped" {
                line = $0
                sub(/,[^,]*$/, "", line)
                sub(/^[^,]*,[^,]*,("[^"]*"|[^,]*),[^,]*,/, "", line)
                gsub(/^"|"$/, "", line)
                gsub(/""/, "\"", line)
                failed[line] = 1
            }
            END {
                for (k in failed) print k
            }
        ' "$OUT_DIR/results.csv" > "$iso_candidates"
        N_ISO_CANDIDATES=$(grep -c . "$iso_candidates" || true)
        N_RUNS=$((BASE_RUNS + N_ISO_CANDIDATES * ISOLATED))
    fi
    iso_count=0
    while IFS= read -r test_id; do
        [[ -n $test_id ]] || continue
        printf '%s\n' "$test_id" > "$single"
        test_file="${test_id%%::*}"
        for ((r = 1; r <= ISOLATED; r++)); do
            if [[ -f "$PROJECT/$test_file" ]]; then
                run_one isolated alone 0 "$single" "$single" "$test_file"
            else
                run_one isolated alone 0 "$single" "$single" "$test_id"
            fi
            iso_count=$((iso_count + 1))
        done
    done < "$iso_candidates"
    say "isolated: $iso_count runs done"
fi
rm -f "$OUT_DIR/orders/single.txt" "$OUT_DIR/orders/smart_isolated.txt"

ELAPSED=$((SECONDS - START))
RAW_LINES=$(cat "$OUT_DIR"/logs/run_*.log 2>/dev/null | wc -l | tr -d ' ')
ROWS=$(($(wc -l < "$OUT_DIR/results.csv") - 1))

say "finished $RUN_ID runs in ${ELAPSED}s: $ROWS result rows, $RAW_LINES raw log lines"
[[ $TOTAL_TIMEOUTS -eq 0 ]] || say "warning: $TOTAL_TIMEOUTS runs timed out"
[[ $TOTAL_CRASHES -eq 0 ]] || say "warning: $TOTAL_CRASHES runs ended abnormally (see $OUT_REL/run_meta.csv)"

if [[ $TOTAL_PARSED -eq 0 ]]; then
    die "no test results could be read from the logs; see $OUT_REL/logs/ for what pytest printed" "$E_RUNS"
fi

say "results:  $OUT_REL/results.csv"
say "run info: $OUT_REL/run_meta.csv"
say "raw logs: $OUT_REL/logs/"

# ------------------------------------------------------- analysis and report

if [[ $REPORT == no ]]; then
    say "--no-report given; stopping after results.csv"
    exit 0
fi

run_analysis_and_report
