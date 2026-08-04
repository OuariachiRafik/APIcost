-- Create the unprivileged role the application connects as.
--
-- This is a security control, not tidiness. Postgres superusers bypass
-- row-level security unconditionally — `FORCE ROW LEVEL SECURITY` does not
-- apply to them. The `postgres` image creates POSTGRES_USER as a superuser, so
-- an application connecting as that role has every RLS policy in the schema
-- silently disabled while still passing review.
--
-- Division of labour:
--   apicost      superuser/owner — runs migrations (DDL), owns the tables
--   apicost_app  LOGIN, DML only, NOSUPERUSER NOBYPASSRLS — what the app uses
--
-- Runs once, at cluster initialization, before any migration.

\set app_password `echo "${APICOST_APP_DB_PASSWORD:-apicost_app}"`

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'apicost_app') THEN
        CREATE ROLE apicost_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;

ALTER ROLE apicost_app WITH PASSWORD :'app_password';

GRANT USAGE ON SCHEMA public TO apicost_app;

-- Tables that already exist (none at init time, but keeps this re-runnable).
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO apicost_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO apicost_app;

-- Tables the migrations are about to create, as owned by `apicost`.
ALTER DEFAULT PRIVILEGES FOR ROLE apicost IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO apicost_app;
ALTER DEFAULT PRIVILEGES FOR ROLE apicost IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO apicost_app;
