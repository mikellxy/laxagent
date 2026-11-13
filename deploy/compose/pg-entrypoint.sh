#!/usr/bin/env bash
set -euo pipefail

config=/run/laxagent/config.json
read_field() {
    local value
    if ! value=$(jq -er --arg key "$1" \
        '.database[$key] | strings | select(length > 0) | select(all(explode[]; . >= 32 and . != 127))' \
        "$config" 2>/dev/null); then
        echo "Invalid config.json: database.$1 must be a nonempty string without control characters" >&2
        exit 1
    fi
    printf '%s' "$value"
}

POSTGRES_DB=$(read_field name)
POSTGRES_USER=$(read_field user)
POSTGRES_PASSWORD=$(read_field password)
export POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD

if [[ "${1:-}" == healthcheck ]]; then
    exec pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
