-- The schema exactly as Hibernate's ddl-auto created it before migrations were introduced.
--
-- A database that already has these tables (every environment that ran an earlier build) is
-- baselined at version 1 instead of running this file: spring.flyway.baseline-on-migrate.
-- A fresh database runs it. Never edit this file once applied; change the schema with a new
-- V<n>__description.sql instead.

CREATE TABLE `category_inventory` (
  `sold_quantity` int NOT NULL,
  `total_quantity` int NOT NULL,
  `unit_amount` bigint NOT NULL,
  `category_id` char(36) NOT NULL,
  `currency` char(3) NOT NULL,
  `event_id` char(36) NOT NULL,
  PRIMARY KEY (`category_id`,`event_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `event_sale_status` (
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `event_id` char(36) NOT NULL,
  `sale_status` varchar(16) NOT NULL,
  PRIMARY KEY (`event_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `hold` (
  `quantity` int NOT NULL,
  `confirmed_at` datetime(6) DEFAULT NULL,
  `created_at` datetime(6) NOT NULL,
  `hold_expires_at` datetime(6) NOT NULL,
  `payment_expires_at` datetime(6) NOT NULL,
  `total_amount` bigint NOT NULL,
  `unit_amount` bigint NOT NULL,
  `correlation_id` varchar(64) DEFAULT NULL,
  `payment_reference` varchar(128) DEFAULT NULL,
  `admission_id` char(36) NOT NULL,
  `category_id` char(36) NOT NULL,
  `currency` char(3) NOT NULL,
  `event_id` char(36) NOT NULL,
  `hold_id` char(36) NOT NULL,
  `order_id` char(36) DEFAULT NULL,
  `user_id` char(36) NOT NULL,
  `user_sub` char(36) DEFAULT NULL,
  `status` varchar(16) NOT NULL,
  PRIMARY KEY (`hold_id`),
  UNIQUE KEY `uk_hold_admission_id` (`admission_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `idempotency_record` (
  `created_at` datetime(6) NOT NULL,
  `request_fingerprint` varchar(64) NOT NULL,
  `idempotency_key` varchar(128) NOT NULL,
  `hold_id` char(36) DEFAULT NULL,
  PRIMARY KEY (`idempotency_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `outbox_message` (
  `attempts` int NOT NULL,
  `occurred_at` datetime(6) NOT NULL,
  `published_at` datetime(6) DEFAULT NULL,
  `schema_version` varchar(16) NOT NULL,
  `correlation_id` varchar(64) DEFAULT NULL,
  `event_type` varchar(64) NOT NULL,
  `aggregate_id` char(36) NOT NULL,
  `message_id` char(36) NOT NULL,
  `payload` text NOT NULL,
  PRIMARY KEY (`message_id`),
  KEY `idx_outbox_unpublished` (`published_at`,`occurred_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
