#!/bin/sh
# Monthly restore drill: restore the latest backup of one site into a scratch directory and a scratch
# database, then remove both. Run as root on a CloudPanel server; the scratch database must exist
# (create it once with: clpctl db:add --domainName=... --databaseName=restore_drill ...).
set -eu

SITE="${1:?usage: restore-drill.sh <domain> <source-database>}"
SOURCE_DB="${2:?usage: restore-drill.sh <domain> <source-database>}"
SCRATCH="/var/tmp/restore-drill/$SITE"

rm -rf "$SCRATCH"
cloudpanel-backup --env-file /etc/cloudpanel-backup/env verify --site "$SITE" --deep
cloudpanel-backup --env-file /etc/cloudpanel-backup/env restore "$SITE" \
  --target "$SCRATCH" --db-map "$SOURCE_DB=restore_drill" --no-chown
ls -la "$SCRATCH" | head
rm -rf "$SCRATCH"
echo "restore drill for $SITE passed"
