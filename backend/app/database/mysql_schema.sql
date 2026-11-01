CREATE TABLE IF NOT EXISTS app_deleted_orders (
 shop_id VARCHAR(64) NOT NULL, order_id VARCHAR(128) NOT NULL, deleted_at VARCHAR(40) NOT NULL,
 PRIMARY KEY(shop_id,order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS sync_runs (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, shop_id VARCHAR(64) NOT NULL,
 started_at VARCHAR(40) NOT NULL, start_date VARCHAR(40) NOT NULL, end_date VARCHAR(40) NOT NULL,
 row_count INT NOT NULL, origin VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS customers (
 customer_key VARCHAR(191) NOT NULL PRIMARY KEY, shop_id VARCHAR(64) NOT NULL,
 display_name TEXT, phone VARCHAR(128), username_candidate VARCHAR(191),
 username VARCHAR(191) COLLATE utf8mb4_unicode_ci,
 verified INT NOT NULL DEFAULT 0, opted_out INT NOT NULL DEFAULT 0, updated_at VARCHAR(40) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS orders (
 shop_id VARCHAR(64) NOT NULL, order_id VARCHAR(128) NOT NULL, customer_key VARCHAR(191) NOT NULL,
 status INT, source VARCHAR(191), amount DOUBLE, amount_source VARCHAR(128),
 created_at VARCHAR(40), updated_at VARCHAR(40), status_at VARCHAR(40), update_date VARCHAR(40),
 date_run VARCHAR(40), raw_json LONGTEXT NOT NULL, synced_at VARCHAR(40) NOT NULL,
 PRIMARY KEY(shop_id,order_id), INDEX idx_orders_customer(customer_key),
 FOREIGN KEY(customer_key) REFERENCES customers(customer_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS order_items (
 shop_id VARCHAR(64) NOT NULL, order_id VARCHAR(128) NOT NULL, line_no INT NOT NULL,
 sku TEXT, product_name TEXT, quantity DOUBLE, unit_price DOUBLE, total_discount DOUBLE, raw_json LONGTEXT NOT NULL,
 PRIMARY KEY(shop_id,order_id,line_no),
 FOREIGN KEY(shop_id,order_id) REFERENCES orders(shop_id,order_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS app_order_prices (
 shop_id VARCHAR(64) NOT NULL, order_id VARCHAR(128) NOT NULL, price_eligible INT NOT NULL,
 rules_hash VARCHAR(64) NOT NULL, updated_at VARCHAR(40) NOT NULL,
 PRIMARY KEY(shop_id,order_id), INDEX idx_order_prices_eligible(price_eligible,rules_hash),
 FOREIGN KEY(shop_id,order_id) REFERENCES orders(shop_id,order_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS sync_run_orders (
 sync_run_id BIGINT NOT NULL, shop_id VARCHAR(64) NOT NULL, order_id VARCHAR(128) NOT NULL, position INT NOT NULL,
 PRIMARY KEY(sync_run_id,shop_id,order_id), FOREIGN KEY(sync_run_id) REFERENCES sync_runs(id),
 FOREIGN KEY(shop_id,order_id) REFERENCES orders(shop_id,order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS status_events (
 shop_id VARCHAR(64) NOT NULL, order_id VARCHAR(128) NOT NULL, line_no INT NOT NULL,
 status INT, updated_at VARCHAR(40), raw_json LONGTEXT NOT NULL,
 PRIMARY KEY(shop_id,order_id,line_no),
 FOREIGN KEY(shop_id,order_id) REFERENCES orders(shop_id,order_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS messages (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, campaign VARCHAR(128) NOT NULL,
 shop_id VARCHAR(64) NOT NULL, order_id VARCHAR(128) NOT NULL, customer_key VARCHAR(191) NOT NULL,
 username VARCHAR(191) COLLATE utf8mb4_unicode_ci NOT NULL, body LONGTEXT NOT NULL,
 state VARCHAR(20) NOT NULL DEFAULT 'pending', created_at VARCHAR(40) NOT NULL,
 attempted_at VARCHAR(40), finished_at VARCHAR(40), error TEXT,
 require_verified INT NOT NULL DEFAULT 1, sync_run_id BIGINT,
 CHECK(state IN ('pending','sending','submitted','sent','failed','uncertain','cancelled')),
 UNIQUE KEY uq_messages_recipient(campaign,shop_id,order_id,username),
 INDEX idx_messages_username(username,state,attempted_at),
 FOREIGN KEY(shop_id,order_id) REFERENCES orders(shop_id,order_id),
 FOREIGN KEY(customer_key) REFERENCES customers(customer_key),
 FOREIGN KEY(sync_run_id) REFERENCES sync_runs(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS message_events (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, message_id BIGINT NOT NULL,
 at VARCHAR(40) NOT NULL, state VARCHAR(20) NOT NULL, note TEXT,
 FOREIGN KEY(message_id) REFERENCES messages(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS app_meta (`key` VARCHAR(191) PRIMARY KEY, value LONGTEXT NOT NULL)
 ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS app_schedule (
 id INT PRIMARY KEY, payload LONGTEXT NOT NULL, CHECK(id=1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS app_jobs (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, created_at VARCHAR(40) NOT NULL,
 started_at VARCHAR(40), finished_at VARCHAR(40), source VARCHAR(64) NOT NULL, mode VARCHAR(32) NOT NULL,
 state VARCHAR(32) NOT NULL, requested INT NOT NULL DEFAULT 0, submitted INT NOT NULL DEFAULT 0,
 summary LONGTEXT NOT NULL, error TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS app_opt_out_hash (digest VARCHAR(64) PRIMARY KEY, created_at VARCHAR(40) NOT NULL)
 ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS app_customer_profiles (
 `key` VARCHAR(500) PRIMARY KEY, username VARCHAR(191), display_name TEXT, phone VARCHAR(128),
 tags LONGTEXT NOT NULL, customer_keys LONGTEXT NOT NULL, updated_at VARCHAR(40) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE OR REPLACE VIEW customer_stats AS
 SELECT c.*,COALESCE(s.order_count,0) AS order_count,COALESCE(s.status3_orders,0) AS status3_orders,
 COALESCE(s.observed_order_value,0) AS observed_order_value,s.last_order_date
 FROM customers c LEFT JOIN (
 SELECT customer_key,COUNT(*) AS order_count,SUM(CASE WHEN status=3 THEN 1 ELSE 0 END) AS status3_orders,
 SUM(amount) AS observed_order_value,MAX(update_date) AS last_order_date FROM orders GROUP BY customer_key
 ) s ON c.customer_key=s.customer_key;
