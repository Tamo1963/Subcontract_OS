#!/usr/bin/env bash
# Subcontract OS - create PostgreSQL role/database with a TEMPORARY password, write .env, migrate, load demo.
# Usage: scripts/setup_postgres.sh [--no-demo]   (env: APP_USER, APP_DB, VALID_DAYS, PGHOST, PGPORT, PGSUPERUSER)
set -euo pipefail
cd "$(dirname "$0")/.."
APP_USER=${APP_USER:-scos_app}; APP_DB=${APP_DB:-subcontract_os}; VALID_DAYS=${VALID_DAYS:-30}
PGHOST=${PGHOST:-localhost}; PGPORT=${PGPORT:-5432}; PGSUPERUSER=${PGSUPERUSER:-postgres}
APP_PASSWORD="Tmp$(python3 -c 'import secrets,string;a=string.ascii_letters+string.digits;print("".join(secrets.choice(a) for _ in range(18)))')"
psql -h "$PGHOST" -p "$PGPORT" -U "$PGSUPERUSER" -d postgres -q -v app_user="$APP_USER" -v app_password="$APP_PASSWORD" \
     -v app_db="$APP_DB" -v valid_days="$VALID_DAYS" -f db/setup_postgres.sql
[ -f .env ] && cp .env .env.bak
cat > .env <<ENV
SCOS_DATABASE_URL=postgresql+psycopg://${APP_USER}:${APP_PASSWORD}@${PGHOST}:${PGPORT}/${APP_DB}
SCOS_AUTO_MIGRATE=1
SCOS_SESSION_HOURS=8
SCOS_ALLOW_DEV_AUTH=0
ENV
chmod 600 .env
python3 -m app.cli migrate
if [ "${1:-}" != "--no-demo" ]; then python3 -m seed.make_samples && python3 -m seed.demo_seed; mkdir -p var && touch var/.demo_loaded
else python3 -m app.cli create-admin admin; fi
echo; echo "PostgreSQL ready: $APP_DB on $PGHOST:$PGPORT  user=$APP_USER  password=$APP_PASSWORD (TEMPORARY, expires in $VALID_DAYS days; saved in .env)"
