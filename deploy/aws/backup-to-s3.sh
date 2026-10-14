#!/usr/bin/env bash
# Nightly consistent backup of a Tessera install to S3.
# Stops Tessera for a few seconds, writes a checksummed archive with `tessera backup`, starts it again, uploads.
# The archive includes master.key (needed to decrypt stored connection secrets), so the bucket must stay private
# and encrypted; the CloudFormation template creates it that way.
# Usage: backup-to-s3.sh s3://bucket/prefix [compose-dir]
set -euo pipefail
DEST="${1:?usage: backup-to-s3.sh s3://bucket/prefix [compose-dir]}"
DIR="${2:-/opt/tessera}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="tessera-backup-${STAMP}.tar.gz"
cd "$DIR"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.override.yml"
$COMPOSE stop tessera
trap '$COMPOSE start tessera' EXIT
$COMPOSE run --rm --no-deps tessera tessera backup --include-key --out "/data/backups/${FILE}"
$COMPOSE start tessera
trap - EXIT
aws s3 cp "/srv/tessera/data/backups/${FILE}" "${DEST%/}/${FILE}" --only-show-errors
# keep the last three archives on the instance; S3 lifecycle handles the rest
ls -1t /srv/tessera/data/backups/tessera-backup-*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
echo "backup uploaded: ${DEST%/}/${FILE}"
