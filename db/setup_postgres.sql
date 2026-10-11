-- Subcontract OS: create the application role and database with a TEMPORARY password.
-- Run as a PostgreSQL superuser, e.g.
--   psql -U postgres -v app_user=scos_app -v app_password='...' -v app_db=subcontract_os -v valid_days=30 -f db/setup_postgres.sql
-- The password expires after :valid_days days (VALID UNTIL). Rotate it with:
--   ALTER ROLE scos_app PASSWORD '<new>' VALID UNTIL 'infinity';
\set ON_ERROR_STOP on

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L VALID UNTIL %L', :'app_user', :'app_password',
              (now() + make_interval(days => :valid_days))::text)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec

SELECT format('ALTER ROLE %I LOGIN PASSWORD %L VALID UNTIL %L', :'app_user', :'app_password',
              (now() + make_interval(days => :valid_days))::text) \gexec

SELECT format('CREATE DATABASE %I OWNER %I ENCODING ''UTF8'' TEMPLATE template0', :'app_db', :'app_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'app_db') \gexec

SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'app_db') \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', :'app_db', :'app_user') \gexec

\connect :app_db
SELECT format('ALTER SCHEMA public OWNER TO %I', :'app_user') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
