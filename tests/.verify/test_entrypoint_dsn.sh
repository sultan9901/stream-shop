#!/usr/bin/env bash
# The entrypoint parses host/port out of DATABASE_URL with pure shell substitution.
# A bug there makes the container hang for 60s and then exit 75, so it is worth
# proving against every DSN shape this project documents. `pg_isready` is stubbed
# so we can observe exactly which host/port the real function would probe.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BASH_BIN="$(command -v bash)"
PASS=0; FAIL=0

probe() {
    local name="$1" url="$2" want="$3"
    local out rc
    out=$(env -i "PATH=$PATH" "$BASH_BIN" -c "
        source '$HERE/entrypoint_funcs.sh'
        pg_isready() {                       # stub: report what the real binary was asked
            local host port
            while [[ \$# -gt 0 ]]; do
                case \$1 in --host=*) host=\${1#*=};; --port=*) port=\${1#*=};; esac
                shift
            done
            printf 'PROBED %s:%s\n' \"\$host\" \"\$port\"
            return 0
        }
        DATABASE_URL='$url' wait_for_db
    " 2>&1)
    rc=$?
    if [[ "$want" == "skip" ]]; then
        if [[ $rc -eq 0 && "$out" != *PROBED* ]]; then
            printf 'PASS  %-46s -> not a postgres DSN, skipped\n' "$name"; PASS=$((PASS+1))
        else
            printf 'FAIL  %-46s -> should have skipped; rc=%s out=%s\n' "$name" "$rc" "${out//$'\n'/ | }"; FAIL=$((FAIL+1))
        fi
    elif [[ "$out" == *"PROBED $want"* ]]; then
        printf 'PASS  %-46s -> %s\n' "$name" "$want"; PASS=$((PASS+1))
    else
        printf 'FAIL  %-46s -> expected %s, got %s\n' "$name" "$want" "${out//$'\n'/ | }"; FAIL=$((FAIL+1))
    fi
}

echo "--- DSN shapes that must resolve to the right host:port ---"
probe "compose default (asyncpg, no port)"      "postgresql+asyncpg://stream:pass@db/stream_corporation"          "db:5432"
probe "compose default with explicit port"      "postgresql+asyncpg://stream:pass@db:5432/stream_corporation"     "db:5432"
probe "non-standard port"                       "postgresql+asyncpg://stream:pass@db:6543/stream_corporation"     "db:6543"
probe "bare postgresql:// scheme"               "postgresql://stream:pass@10.0.0.5:5432/stream"                   "10.0.0.5:5432"
probe "postgres:// short scheme"                "postgres://stream:pass@pg.internal:5432/stream"                  "pg.internal:5432"
probe "fully-qualified managed host"            "postgresql+asyncpg://u:p@db.abc123.eu-west-1.rds.amazonaws.com:5432/stream" "db.abc123.eu-west-1.rds.amazonaws.com:5432"
probe "password containing an @ (url-encoded)"  "postgresql+asyncpg://stream:p%40ss@db:5432/stream"               "db:5432"
probe "DSN with query parameters"               "postgresql+asyncpg://stream:pass@db:5432/stream?ssl=require"     "db:5432"

echo
echo "--- non-postgres DSNs must be skipped, not probed ---"
probe "sqlite (the dev default)"                "sqlite+aiosqlite:///./stream_corporation.db"                     skip
probe "empty DATABASE_URL"                      ""                                                               skip

echo
printf 'DSN PARSING: %s/%s checks passed\n' "$PASS" "$((PASS+FAIL))"
[[ $FAIL -eq 0 ]] || exit 1
