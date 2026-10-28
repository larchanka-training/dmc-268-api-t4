#!/bin/sh
# Streaming standby of the production PostgreSQL (docs/devops.md, «Реплика PostgreSQL»).
#
# Runs as the postgres-replica service of deploy/compose.yml in the postgres:17-alpine image.
# On the first start (empty data directory) it copies the primary with pg_basebackup over the
# db network and writes standby.signal and primary_conninfo (-R); after that it only starts
# PostgreSQL, which keeps replaying WAL from the primary. The physical slot `replica1` makes
# the primary keep WAL the standby has not received yet; postgres caps it with
# max_slot_wal_keep_size, so a dead standby cannot fill the disk.
#
# The replica is read-only. To rebuild it (for example after a primary restore), remove the
# pgreplica volume and the slot: docs/devops.md, «Реплика PostgreSQL».
#
# ЗАГЛУШКА(С4): реплика на том же VPS, что и primary (решение 27.10: пока так, потом второй
# сервер). От потери сервера не защищает. ЖДЁМ второй сервер от владельца: тогда этот сервис
# переезжает туда, PGHOST становится адресом primary, а порт 5432 primary открывается только
# для IP второго сервера.
set -eu

: "${PGHOST:?PGHOST must be set}"
: "${PGUSER:?PGUSER must be set}"
: "${PGPASSWORD:?PGPASSWORD must be set}"
SLOT=${REPLICA_SLOT:-replica1}
PGDATA=${PGDATA:-/var/lib/postgresql/data}
export PGDATA

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) replica: $*"; }

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -q -h "$PGHOST"; do
    log "waiting for $PGHOST"
    sleep 2
  done
  # A half-finished copy from an interrupted first start is useless: start over.
  rm -rf "${PGDATA:?}"/* "${PGDATA:?}"/.[!.]* 2>/dev/null || true
  install -d -o postgres -g postgres -m 700 "$PGDATA"
  log "copying $PGHOST with pg_basebackup, slot $SLOT"
  # -C creates the slot; when it is already there (the volume was removed, the slot was not)
  # the copy reuses it.
  if ! su-exec postgres pg_basebackup -h "$PGHOST" -U "$PGUSER" -D "$PGDATA" \
      -X stream -R -C -S "$SLOT" --checkpoint=fast; then
    rm -rf "${PGDATA:?}"/* "${PGDATA:?}"/.[!.]* 2>/dev/null || true
    su-exec postgres pg_basebackup -h "$PGHOST" -U "$PGUSER" -D "$PGDATA" \
      -X stream -R -S "$SLOT" --checkpoint=fast
  fi
  log "copy done"
fi

# The image entrypoint sees an initialised data directory, skips initdb and runs postgres as
# the postgres user; standby.signal keeps it in recovery.
exec docker-entrypoint.sh postgres -c hot_standby=on
