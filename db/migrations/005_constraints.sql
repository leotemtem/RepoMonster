CREATE UNIQUE INDEX standard_documents_one_enabled_version
    ON standard_documents (logical_key)
    WHERE enabled AND scope IN ('repo', 'task');
