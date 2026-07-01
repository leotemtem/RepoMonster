CREATE TABLE webhook_deliveries (
    id bigserial PRIMARY KEY,
    provider text NOT NULL CHECK (provider IN ('github', 'gitlab')),
    delivery_id text NOT NULL,
    event_name text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'completed', 'failed', 'ignored')
    ),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    locked_at timestamptz,
    external_result_id text,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, delivery_id)
);

CREATE INDEX webhook_deliveries_pending_idx
    ON webhook_deliveries (available_at, id)
    WHERE status = 'pending';
