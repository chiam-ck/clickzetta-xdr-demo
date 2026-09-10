-- Synthetic TMF-like analytical projection, not a TMF API implementation.
-- Fresh schema required. No IF NOT EXISTS: never silently reuse an old dataset.
CREATE SCHEMA {{schema}};
CREATE TABLE {{schema}}.party (
  party_id BIGINT PRIMARY KEY, party_type STRING, display_name STRING
);
CREATE TABLE {{schema}}.customer (
  customer_id BIGINT PRIMARY KEY, party_id BIGINT, status STRING,
  marketing_consent BOOLEAN
);
CREATE TABLE {{schema}}.billing_account (
  account_id BIGINT PRIMARY KEY, customer_id BIGINT, currency STRING
);
CREATE TABLE {{schema}}.subscriber (
  subscriber_id BIGINT PRIMARY KEY, party_id BIGINT, customer_id BIGINT
);
CREATE TABLE {{schema}}.product_offering (
  offering_id BIGINT PRIMARY KEY, family STRING
);
CREATE TABLE {{schema}}.product_inventory (
  subscription_id BIGINT PRIMARY KEY, customer_id BIGINT, account_id BIGINT,
  subscriber_id BIGINT, offering_id BIGINT, status STRING,
  monthly_charge DECIMAL(12,2), contract_end DATE, renewal_due BOOLEAN
);
CREATE TABLE {{schema}}.customer_bill (
  bill_id BIGINT PRIMARY KEY, account_id BIGINT, due_date DATE,
  outstanding_amount DECIMAL(12,2), overdue_30 BOOLEAN
);
CREATE TABLE {{schema}}.trouble_ticket (
  ticket_id BIGINT PRIMARY KEY, customer_id BIGINT, status STRING, priority STRING
);
