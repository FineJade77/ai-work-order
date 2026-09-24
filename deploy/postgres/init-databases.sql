-- One Postgres instance, one database per service. Safe to re-run.
-- Local development only: in the cloud, databases and roles come from infrastructure
-- provisioning with passwords from the secret manager, not from this file.
--
-- Per service:  <svc>_owner  owns the database, runs migrations and seeds
--               <svc>_app    runtime account; not an owner, so FORCE ROW LEVEL SECURITY applies

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', r.name, r.password)
FROM (VALUES
    ('app_service_owner',  'app_service_owner_dev'),
    ('app_service_app',    'app_service_app_dev'),
    ('tool_gateway_owner', 'tool_gateway_owner_dev'),
    ('tool_gateway_app',   'tool_gateway_app_dev'),
    ('ai_gateway_owner',   'ai_gateway_owner_dev'),
    ('ai_gateway_app',     'ai_gateway_app_dev'),
    ('ticket_service_owner', 'ticket_service_owner_dev'),
    ('ticket_service_app',   'ticket_service_app_dev')
) AS r(name, password)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r.name)
\gexec

SELECT format('CREATE DATABASE %I OWNER %I', d.name, d.owner)
FROM (VALUES
    ('app_service',  'app_service_owner'),
    ('tool_gateway', 'tool_gateway_owner'),
    ('ai_gateway',   'ai_gateway_owner'),
    ('ticket_service', 'ticket_service_owner')
) AS d(name, owner)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = d.name)
\gexec

-- Each runtime account can reach only its own service's database.
REVOKE CONNECT ON DATABASE app_service, tool_gateway, ai_gateway, ticket_service FROM PUBLIC;
GRANT CONNECT ON DATABASE app_service TO app_service_app;
GRANT CONNECT ON DATABASE tool_gateway TO tool_gateway_app;
GRANT CONNECT ON DATABASE ai_gateway TO ai_gateway_app;
GRANT CONNECT ON DATABASE ticket_service TO ticket_service_app;
