-- Each section is also a CTE in the full-recompute control.
-- @query subscription_features
SELECT p.customer_id,
       SUM(CASE WHEN p.status = 'active' THEN 1 ELSE 0 END) AS active_subscriptions,
       SUM(CASE WHEN p.status = 'active' THEN p.monthly_charge ELSE 0 END) AS monthly_spend,
       SUM(CASE WHEN p.status = 'active' AND o.family = 'mobile' THEN 1 ELSE 0 END) AS mobile_lines,
       SUM(CASE WHEN p.status = 'active' AND o.family = 'broadband' THEN 1 ELSE 0 END) AS broadband_lines,
       SUM(CASE WHEN p.status = 'active' AND p.renewal_due THEN 1 ELSE 0 END) AS renewal_lines
FROM {{schema}}.product_inventory p
JOIN {{schema}}.product_offering o ON p.offering_id = o.offering_id
GROUP BY p.customer_id
-- @query billing_features
SELECT a.customer_id,
       SUM(CASE WHEN b.overdue_30 THEN b.outstanding_amount ELSE 0 END) AS overdue_amount
FROM {{schema}}.customer_bill b
JOIN {{schema}}.billing_account a ON b.account_id = a.account_id
GROUP BY a.customer_id
-- @query care_features
SELECT customer_id,
       SUM(CASE WHEN status = 'open' AND priority = 'high' THEN 1 ELSE 0 END) AS open_complaints
FROM {{schema}}.trouble_ticket
GROUP BY customer_id
-- @query customer_features
SELECT c.customer_id, c.status, c.marketing_consent,
       COALESCE(s.active_subscriptions, 0) AS active_subscriptions,
       COALESCE(s.monthly_spend, 0) AS monthly_spend,
       COALESCE(s.mobile_lines, 0) AS mobile_lines,
       COALESCE(s.broadband_lines, 0) AS broadband_lines,
       COALESCE(s.renewal_lines, 0) AS renewal_lines,
       COALESCE(b.overdue_amount, 0) AS overdue_amount,
       COALESCE(t.open_complaints, 0) AS open_complaints
FROM {{schema}}.customer c
LEFT JOIN {{schema}}.subscription_features s ON c.customer_id = s.customer_id
LEFT JOIN {{schema}}.billing_features b ON c.customer_id = b.customer_id
LEFT JOIN {{schema}}.care_features t ON c.customer_id = t.customer_id
-- @query customer_tags
SELECT customer_id,
       'bss-v1' AS rule_version,
       CASE WHEN status = 'active' AND monthly_spend >= 100 THEN 1 ELSE 0 END AS high_value,
       CASE WHEN status = 'active' AND active_subscriptions >= 2 THEN 1 ELSE 0 END AS multi_line,
       CASE WHEN status = 'active' AND overdue_amount >= 50 THEN 1 ELSE 0 END AS payment_risk,
       CASE WHEN status = 'active' AND monthly_spend >= 100
                  AND (renewal_lines > 0 OR open_complaints > 0)
            THEN 1 ELSE 0 END AS retention_priority,
       CASE WHEN status = 'active' AND marketing_consent = TRUE
                  AND mobile_lines > 0 AND broadband_lines = 0
                  AND overdue_amount = 0 AND open_complaints = 0
            THEN 1 ELSE 0 END AS broadband_cross_sell,
       CASE WHEN status <> 'active' OR status IS NULL
                  OR marketing_consent = FALSE OR marketing_consent IS NULL
                  OR overdue_amount > 0 OR open_complaints > 0
            THEN 1 ELSE 0 END AS campaign_suppressed
FROM {{schema}}.customer_features
