#!/usr/bin/env bash
set -euo pipefail

pg_bindir="$(pg_config --bindir)"
pg_tmp="$(mktemp -d /tmp/hnh-postgres.XXXXXX)"
pg_data="$pg_tmp/data"
pg_socket="$pg_tmp/socket"
pg_port="55439"
database_name="hnh_test"

mkdir -p "$pg_socket"

cleanup() {
  if [[ -f "$pg_data/postmaster.pid" ]]; then
    "$pg_bindir/pg_ctl" -D "$pg_data" -m fast -w stop >/dev/null
  fi
  rm -rf "$pg_tmp"
}
trap cleanup EXIT INT TERM

"$pg_bindir/initdb" -D "$pg_data" --auth=trust --no-locale --encoding=UTF8 >/dev/null
"$pg_bindir/pg_ctl" -D "$pg_data" -o "-F -k $pg_socket -p $pg_port" -w start >/dev/null
"$pg_bindir/createdb" -h "$pg_socket" -p "$pg_port" "$database_name"

export HNH_TEST_DATABASE_URL="postgresql+psycopg:///$database_name?host=$pg_socket&port=$pg_port"
export HNH_DATABASE_URL="$HNH_TEST_DATABASE_URL"

.venv/bin/alembic upgrade head
.venv/bin/alembic downgrade base
.venv/bin/alembic upgrade head
.venv/bin/alembic check
.venv/bin/pytest "$@"
