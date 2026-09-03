#!/usr/bin/env bash
# Real execution of the container entrypoint's production gate. The function bodies
# are the ones shipped in docker/entrypoint.sh (extracted verbatim), so a pass here
# means the real container would behave the same way.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BASH_BIN="$(command -v bash)"
PASS=0; FAIL=0

run_case() {
    local name="$1" want="$2"; shift 2
    local out rc
    # env -i gives each case a pristine environment (PATH only) so a leaked
    # variable from this shell cannot make a gate look like it passed.
    out=$(env -i "PATH=$PATH" "$BASH_BIN" -c "source '$HERE/entrypoint_funcs.sh'; $* validate_config" 2>&1)
    rc=$?
    if [[ "$want" == "reject" ]]; then
        if [[ $rc -eq 78 ]]; then
            printf 'PASS  %s -> refused with EX_CONFIG(78)\n' "$name"; PASS=$((PASS+1))
        else
            printf 'FAIL  %s -> expected exit 78, got %s\n      %s\n' "$name" "$rc" "${out//$'\n'/ | }"; FAIL=$((FAIL+1))
        fi
    elif [[ "$want" == "accept" ]]; then
        if [[ $rc -eq 0 ]]; then
            printf 'PASS  %s -> accepted\n' "$name"; PASS=$((PASS+1))
        else
            printf 'FAIL  %s -> expected exit 0, got %s\n      %s\n' "$name" "$rc" "${out//$'\n'/ | }"; FAIL=$((FAIL+1))
        fi
    else   # want=warn: must exit 0 AND print the given marker
        if [[ $rc -eq 0 && "$out" == *"$want"* ]]; then
            printf 'PASS  %s -> warned, still booted\n' "$name"; PASS=$((PASS+1))
        else
            printf 'FAIL  %s -> rc=%s out=%s\n' "$name" "$rc" "${out//$'\n'/ | }"; FAIL=$((FAIL+1))
        fi
    fi
}

GOOD="ENVIRONMENT=production SECRET_KEY=$(printf 'k%.0s' {1..48}) COOKIE_SECURE=true ALLOWED_HOSTS=stream.example.com"

echo "--- development mode must never block a boot ---"
run_case "development with every unsafe default"            accept "ENVIRONMENT=development"
run_case "unset ENVIRONMENT (defaults to development)"      accept ""

echo
echo "--- production must refuse an unsafe configuration ---"
run_case "production, SECRET_KEY missing"                   reject "ENVIRONMENT=production COOKIE_SECURE=true ALLOWED_HOSTS=x.com"
run_case "production, SECRET_KEY only 31 chars"             reject "ENVIRONMENT=production SECRET_KEY=$(printf 'k%.0s' {1..31}) COOKIE_SECURE=true ALLOWED_HOSTS=x.com"
run_case "production, COOKIE_SECURE=false"                  reject "ENVIRONMENT=production SECRET_KEY=$(printf 'k%.0s' {1..48}) COOKIE_SECURE=false ALLOWED_HOSTS=x.com"
run_case "production, COOKIE_SECURE unset"                  reject "ENVIRONMENT=production SECRET_KEY=$(printf 'k%.0s' {1..48}) ALLOWED_HOSTS=x.com"
run_case "production, ALLOWED_HOSTS still '*'"              reject "ENVIRONMENT=production SECRET_KEY=$(printf 'k%.0s' {1..48}) COOKIE_SECURE=true ALLOWED_HOSTS='*'"
run_case "production, ALLOWED_HOSTS unset (defaults to *)"  reject "ENVIRONMENT=production SECRET_KEY=$(printf 'k%.0s' {1..48}) COOKIE_SECURE=true"
run_case "production, all three wrong at once"              reject "ENVIRONMENT=production"

echo
echo "--- production must accept a correct configuration ---"
run_case "production, SECRET_KEY 48 chars + secure + hosts" accept "$GOOD"
run_case "production, SECRET_KEY exactly 32 chars"          accept "ENVIRONMENT=production SECRET_KEY=$(printf 'k%.0s' {1..32}) COOKIE_SECURE=true ALLOWED_HOSTS=x.com"

echo
echo "--- production must warn (not block) on risky-but-legal settings ---"
run_case "bootstrap master password still 'admin'"          "still 'admin'"        "$GOOD DEFAULT_MASTER_PASSWORD=admin"
run_case "SQLite DSN in production"                         "points at SQLite"     "$GOOD DATABASE_URL=sqlite+aiosqlite:///./x.db"
run_case "multi-worker with no REDIS_URL"                   "per-worker"           "$GOOD WEB_CONCURRENCY=4 REDIS_URL="
run_case "multi-worker WITH REDIS_URL (no warning)"         accept                 "$GOOD WEB_CONCURRENCY=4 REDIS_URL=redis://redis:6379/0"

echo
printf 'ENTRYPOINT GATE: %s/%s checks passed\n' "$PASS" "$((PASS+FAIL))"
[[ $FAIL -eq 0 ]] || exit 1
