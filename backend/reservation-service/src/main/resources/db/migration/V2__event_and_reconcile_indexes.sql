-- The primary key of category_inventory leads with category_id, so nothing by event alone could
-- use it. The confirmation locks an event's categories (FOR UPDATE); without this index that
-- locking read scans the table and locks every event's rows, making all confirmations queue up.
CREATE INDEX `idx_category_inventory_event` ON `category_inventory` (`event_id`, `category_id`);

-- Reconciliation looks for ACTIVE holds past their deadline every minute; cancellation and the
-- rebuild read an event's holds.
CREATE INDEX `idx_hold_status_expires` ON `hold` (`status`, `hold_expires_at`);
CREATE INDEX `idx_hold_event` ON `hold` (`event_id`);
