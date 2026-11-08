# parse_log.awk - turn one verbose pytest log into results.csv rows.
#
# Usage:
#   awk -v run_id=N -v condition=C -v cvalue=V -v timed_out=0|1 \
#       -f parse_log.awk EXPECTED_IDS_FILE RUN_LOG
#
# EXPECTED_IDS_FILE lists the node IDs this run was meant to execute, one per
# line, in the order they were requested. RUN_LOG is the output of
# `pytest -v`, whose result lines look like
#   tests/test_x.py::test_a[some id] PASSED                 [ 33%]
#   tests/test_x.py::test_b SKIPPED (reason)                [ 66%]
#
# Rows printed:  run_id,condition,condition_value,position,test_id,outcome
#   position  order in which the test reported a result in this run (empty if
#             it never reported one)
#   outcome   passed|failed|error|skipped|xfailed|xpassed|timeout|not_run
#
# The node ID is matched against the expected list instead of being split on
# spaces, because parametrised IDs can contain spaces, dashes and brackets.

function csv_quote(s) {
    gsub(/"/, "\"\"", s)
    return "\"" s "\""
}

# Higher rank wins when a test reports more than once (e.g. PASSED in the call
# phase, then ERROR in teardown).
function rank(o) {
    if (o == "error")  return 3
    if (o == "failed") return 2
    return 1
}

BEGIN {
    n_status = split("PASSED FAILED ERROR SKIPPED XFAIL XPASS", status_word, " ")
    outcome_for["PASSED"] = "passed"
    outcome_for["FAILED"] = "failed"
    outcome_for["ERROR"]  = "error"
    outcome_for["SKIPPED"] = "skipped"
    outcome_for["XFAIL"]  = "xfailed"
    outcome_for["XPASS"]  = "xpassed"
    n_expected = 0
    n_reported = 0
}

# First file: the expected node IDs.
FNR == NR {
    sub(/\r$/, "")
    if ($0 != "" && !($0 in expected)) {
        expected[$0] = ++n_expected
        expected_order[n_expected] = $0
    }
    next
}

# Second file: the pytest log. Result lines appear only between the
# "test session starts" banner and the next pytest section banner (FAILURES,
# ERRORS, warnings summary, short test summary, or final summary).
{ sub(/\r$/, "") }
results_done { next }
!in_results {
    if ($0 ~ /^=+ test session starts =+$/) in_results = 1
    next
}
/^=+ (FAILURES|ERRORS|warnings summary|short test summary info|slowest .* durations|[0-9]+ (passed|failed|error|skipped|xfailed|xpassed|warnings?|deselected).* in [0-9.]+s.*) =+$/ {
    results_done = 1
    next
}

# Try every " STATUS" occurrence on the line and accept the first whose
# left-hand side is an expected node ID.
{
    line = $0
    for (s = 1; s <= n_status; s++) {
        word = " " status_word[s]
        offset = 0
        rest = line
        while ((p = index(rest, word)) > 0) {
            after = substr(rest, p + length(word), 1)
            candidate = substr(line, 1, offset + p - 1)
            if ((after == "" || after == " ") && (candidate in expected)) {
                o = outcome_for[status_word[s]]
                if (!(candidate in outcome)) {
                    outcome[candidate] = o
                    position[candidate] = ++n_reported
                } else if (rank(o) > rank(outcome[candidate])) {
                    outcome[candidate] = o
                }
                next
            }
            offset += p + length(word) - 1
            rest = substr(line, offset + 1)
        }
    }
}

END {
    missing_seen = 0
    for (i = 1; i <= n_expected; i++) {
        id = expected_order[i]
        if (id in outcome) {
            o = outcome[id]
            pos = position[id]
        } else {
            # pytest prints a result only after a test finishes, so after a
            # timeout the first missing test is the one that was running.
            o = (timed_out == 1 && missing_seen == 0) ? "timeout" : "not_run"
            missing_seen = 1
            pos = ""
        }
        print run_id "," condition "," csv_quote(cvalue) "," pos "," csv_quote(id) "," o
    }
}
