#!/usr/bin/env bash
set -euo pipefail

backup_dir="${1:-backups/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$backup_dir"
docker compose exec -T postgres pg_dump -U finbot -Fc finbot > "$backup_dir/finbot.dump"
docker compose exec -T evolution_db pg_dump -U evolution -Fc evolution > "$backup_dir/evolution.dump"
docker compose cp evolution:/evolution/instances "$backup_dir/evolution-instances"
echo "Backup salvo em $backup_dir"
