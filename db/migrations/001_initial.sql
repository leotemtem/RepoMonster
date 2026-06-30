CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE application_settings (
    key text PRIMARY KEY,
    value text NOT NULL
);

INSERT INTO application_settings (key, value)
VALUES ('embedding_dimensions', '__EMBEDDING_DIMENSIONS__');

CREATE TABLE provider_installations (
    id bigserial PRIMARY KEY,
    provider text NOT NULL CHECK (provider IN ('github', 'gitlab')),
    provider_base_url text NOT NULL,
    external_installation_id text NOT NULL,
    namespace text NOT NULL,
    auth_reference text NOT NULL,
    webhook_secret_reference text,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_base_url, external_installation_id)
);

CREATE TABLE repositories (
    id bigserial PRIMARY KEY,
    installation_id bigint REFERENCES provider_installations(id) ON DELETE SET NULL,
    provider text NOT NULL CHECK (provider IN ('github', 'gitlab')),
    provider_base_url text NOT NULL,
    external_id text NOT NULL,
    repository_key text NOT NULL UNIQUE,
    full_name text NOT NULL,
    default_branch text NOT NULL DEFAULT 'main',
    config_path text NOT NULL DEFAULT '.repomonster.yml',
    config_sha text,
    review_settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_profile text NOT NULL DEFAULT 'default',
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_base_url, external_id)
);

CREATE TABLE standard_packs (
    id text PRIMARY KEY,
    name text NOT NULL,
    description text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE standard_pack_versions (
    pack_id text NOT NULL REFERENCES standard_packs(id) ON DELETE CASCADE,
    version text NOT NULL,
    language text,
    framework text,
    supported_versions text,
    manifest jsonb NOT NULL,
    content_checksum text NOT NULL,
    embedding_model text NOT NULL,
    embedding_dimensions integer NOT NULL,
    status text NOT NULL CHECK (status IN ('indexing', 'ready', 'failed')),
    installed_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pack_id, version)
);

CREATE TABLE repository_pack_selections (
    repository_id bigint NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    pack_id text NOT NULL,
    pack_version text NOT NULL,
    priority integer NOT NULL DEFAULT 100,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repository_id, pack_id),
    FOREIGN KEY (pack_id, pack_version)
        REFERENCES standard_pack_versions(pack_id, version)
);

CREATE TABLE standard_documents (
    id text PRIMARY KEY,
    logical_key text NOT NULL,
    version text NOT NULL,
    scope text NOT NULL CHECK (scope IN ('public', 'company', 'repo', 'task')),
    pack_id text,
    pack_version text,
    repository_key text,
    tenant_key text,
    task_key text,
    language text,
    framework text,
    stack_key text NOT NULL,
    source_path text,
    title text NOT NULL,
    body_markdown text NOT NULL,
    source_urls jsonb NOT NULL DEFAULT '[]'::jsonb,
    tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    content_checksum text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (logical_key, version),
    FOREIGN KEY (pack_id, pack_version)
        REFERENCES standard_pack_versions(pack_id, version),
    CHECK (scope <> 'public' OR (pack_id IS NOT NULL AND pack_version IS NOT NULL)),
    CHECK (scope <> 'company' OR tenant_key IS NOT NULL),
    CHECK (scope <> 'repo' OR repository_key IS NOT NULL),
    CHECK (scope <> 'task' OR (repository_key IS NOT NULL AND task_key IS NOT NULL))
);

CREATE INDEX standard_documents_scope_idx
    ON standard_documents (scope, stack_key, repository_key, tenant_key, task_key)
    WHERE enabled = true;

CREATE TABLE standard_chunks (
    id bigserial PRIMARY KEY,
    document_id text NOT NULL REFERENCES standard_documents(id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    heading text,
    content text NOT NULL,
    content_checksum text NOT NULL,
    token_count integer,
    embedding_model text NOT NULL,
    embedding_dimensions integer NOT NULL,
    embedding vector(__EMBEDDING_DIMENSIONS__) NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, ordinal)
);

CREATE INDEX standard_chunks_embedding_hnsw
    ON standard_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX standard_chunks_document_idx ON standard_chunks (document_id);

CREATE TABLE standard_rules (
    id text NOT NULL,
    document_id text NOT NULL REFERENCES standard_documents(id) ON DELETE CASCADE,
    severity text NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    gate boolean NOT NULL DEFAULT false,
    title text NOT NULL,
    rationale text,
    check_statements jsonb NOT NULL DEFAULT '[]'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (document_id, id)
);

CREATE TABLE repository_standard_sources (
    id bigserial PRIMARY KEY,
    repository_id bigint NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    source_path text NOT NULL,
    source_sha text NOT NULL,
    content_checksum text NOT NULL,
    document_id text REFERENCES standard_documents(id) ON DELETE SET NULL,
    status text NOT NULL CHECK (status IN ('pending', 'ready', 'failed')),
    indexed_at timestamptz,
    UNIQUE (repository_id, source_path)
);

CREATE TABLE review_runs (
    id bigserial PRIMARY KEY,
    repository_id bigint REFERENCES repositories(id) ON DELETE SET NULL,
    provider text NOT NULL CHECK (provider IN ('github', 'gitlab')),
    repository_key text NOT NULL,
    external_id text NOT NULL,
    head_sha text,
    profile_id text NOT NULL,
    gate_state text NOT NULL CHECK (
        gate_state IN ('blocked', 'needs_author_updates', 'ready_for_human_review', 'manual_escalation')
    ),
    request_payload jsonb NOT NULL,
    llm_brief text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, repository_key, external_id, head_sha)
);

CREATE TABLE review_findings (
    id bigserial PRIMARY KEY,
    review_run_id bigint NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    severity text NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    rule_id text,
    category text NOT NULL,
    title text NOT NULL,
    detail text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
