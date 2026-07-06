ALTER TABLE review_runs
    ADD COLUMN insight_recommendation text CHECK (
        insight_recommendation IN ('ready', 'request_changes', 'block')
    ),
    ADD COLUMN insight_recommendation_reason text;

ALTER TABLE review_findings
    ADD COLUMN impact text NOT NULL DEFAULT 'advisory' CHECK (
        impact IN ('advisory', 'significant', 'blocking')
    );
