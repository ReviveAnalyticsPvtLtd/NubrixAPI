CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role NOLOGIN BYPASSRLS;

CREATE DATABASE manual_billing_test_local;
CREATE DATABASE manual_billing_test_live_shape;
CREATE DATABASE manual_billing_test_subscription_model;
CREATE DATABASE pricing_catalog_test_local;
CREATE DATABASE migration_rehearsal_test;
CREATE DATABASE website_visit_test;
CREATE DATABASE website_visit_migration_test;
CREATE DATABASE smoke_test;
