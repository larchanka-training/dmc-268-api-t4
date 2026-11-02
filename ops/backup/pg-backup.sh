#!/bin/sh
# Daily PostgreSQL backup for the team 4 VPS (docs/devops.md, «Бэкапы PostgreSQL»).
#
# Runs as the `backup` service of deploy/compose.yml, in the postgres:17-alpine image, so
# pg_dump always matches the server version. Loop: once an hour, if the newest dump is older
# than BACKUP_INTERVAL_HOURS, write a new one; keep the BACKUP_KEEP newest. Restarts and
# deploys never cause an extra dump, and a missed night is caught up within the hour.
#
# A dump is written to a .partial file, checked with pg_restore --list and only then renamed,
# so a half-written file never counts as a backup.
#
# ЗАГЛУШКА(С4): копия вне сервера. Сейчас бэкапы лежат в volume `pgbackups` на этом же VPS
# (решение Александра 27.10: пока проще) и защищают от ошибок с данными, но не от потери
# сервера. ЖДЁМ: хранилище вне VPS (S3-совместимое: Cloudflare R2, Backblaze B2 или Contabo
# Object Storage) — кто заводит бакет и кладёт ключи в GitHub Secrets. Тогда здесь после
# переименования: шифрование дампа и отправка в бакет.
set -eu

: "${PGHOST:?PGHOST must be set}"
: "${PGUSER:?PGUSER must be set}"
: "${PGPASSWORD:?PGPASSWORD must be set}"
: "${PGDATABASE:?PGDATABASE must be set}"
BACKUP_DIR=${BACKUP_DIR:-/backups}
BACKUP_KEEP=${BACKUP_KEEP:-7}
BACKUP_INTERVAL_HOURS=${BACKUP_INTERVAL_HOURS:-24}
CHECK_SECONDS=${CHECK_SECONDS:-3600}

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

dumps_newest_first() {
  # Finished dumps, one path per line, newest first. find instead of an ls glob: no
  # literal "*.dump" when the directory is empty, and no word splitting on odd names.
  find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump' -exec stat -c '%Y %n' {} + \
    | sort -rn | cut -d' ' -f2-
}

newest_age_seconds() {
  # Prints the age of the newest finished dump, or nothing when there is none.
  newest=$(dumps_newest_first | head -n 1)
  [ -n "$newest" ] || return 0
  echo $(( $(date +%s) - $(stat -c %Y "$newest") ))
}

backup_once() {
  name="$PGDATABASE-$(date -u +%Y%m%dT%H%M%SZ).dump"
  partial="$BACKUP_DIR/$name.partial"
  if ! pg_dump --format=custom --no-owner --file="$partial"; then
    rm -f "$partial"
    log "pg_dump failed"
    return 1
  fi
  if ! pg_restore --list "$partial" >/dev/null; then
    rm -f "$partial"
    log "dump is not readable by pg_restore"
    return 1
  fi
  mv "$partial" "$BACKUP_DIR/$name"
  log "written $name ($(du -h "$BACKUP_DIR/$name" | cut -f1))"
  # Keep the newest BACKUP_KEEP dumps.
  dumps_newest_first | tail -n +"$((BACKUP_KEEP + 1))" | while IFS= read -r old; do
    rm -f "$old"
    log "removed $(basename "$old")"
  done
}

main() {
  mkdir -p "$BACKUP_DIR"
  rm -f "$BACKUP_DIR"/*.partial
  if [ "${1:-}" = "--now" ]; then
    backup_once
    return
  fi
  log "started: every ${BACKUP_INTERVAL_HOURS} h, keep ${BACKUP_KEEP}, in $BACKUP_DIR"
  while true; do
    age=$(newest_age_seconds)
    if [ -z "$age" ] || [ "$age" -ge $((BACKUP_INTERVAL_HOURS * 3600)) ]; then
      backup_once || log "will retry in ${CHECK_SECONDS} s"
    fi
    sleep "$CHECK_SECONDS" &
    wait $!
  done
}

# Exit at once on docker stop instead of waiting out the sleep.
trap 'exit 0' TERM INT
main "$@"
