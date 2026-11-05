ALTER TABLE webhook_deliveries
    ADD COLUMN lock_token uuid,
    ADD COLUMN serial_key text;

UPDATE webhook_deliveries SET serial_key = payload -> 'repository' ->> 'id';

-- Run this migration with the workers stopped. Any job they held is simply retried.
UPDATE webhook_deliveries
SET status = 'pending', locked_at = NULL
WHERE status = 'processing';

ALTER TABLE webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_processing_has_owner CHECK (
        status <> 'processing'
        OR (locked_at IS NOT NULL AND lock_token IS NOT NULL)
    ),
    ADD CONSTRAINT webhook_deliveries_attempts_not_negative CHECK (attempts >= 0);

-- One running job per repository keeps reviews and policy syncs in order.
CREATE UNIQUE INDEX webhook_deliveries_one_running_per_key
    ON webhook_deliveries (provider, serial_key)
    WHERE status = 'processing';
