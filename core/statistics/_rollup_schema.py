"""Disposable aggregate tables maintained by the Statistics rollup owner."""

ROLLUP_SCHEMA = """
CREATE TABLE agg_runs (
    session_key INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    run_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    completion_reason TEXT,
    start_instant INTEGER NOT NULL,
    end_instant INTEGER,
    duration_ms INTEGER,
    iterations INTEGER,
    model_steps INTEGER NOT NULL,
    visible_messages INTEGER NOT NULL,
    user_messages INTEGER NOT NULL,
    first_visible_ms INTEGER,
    tool_calls INTEGER NOT NULL,
    tool_rejected INTEGER NOT NULL,
    tool_ms INTEGER NOT NULL,
    compactions INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    calls INTEGER NOT NULL,
    failed_calls INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    reported_nusd INTEGER NOT NULL,
    estimated_nusd INTEGER NOT NULL,
    unpriced_calls INTEGER NOT NULL,
    primary_model TEXT,
    models TEXT NOT NULL,
    kinds TEXT NOT NULL,
    changed_files INTEGER,
    lines_added INTEGER,
    lines_removed INTEGER,
    PRIMARY KEY (session_key, run_id)
) WITHOUT ROWID;
CREATE INDEX agg_runs_start ON agg_runs(start_instant);
CREATE INDEX agg_runs_origin ON agg_runs(origin, start_instant);
CREATE TABLE agg_run_models (
    session_key INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    model_key TEXT NOT NULL,
    calls INTEGER NOT NULL,
    failed_calls INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    unreported_calls INTEGER NOT NULL,
    reported_nusd INTEGER NOT NULL,
    reported_calls INTEGER NOT NULL,
    estimated_nusd INTEGER NOT NULL,
    estimated_calls INTEGER NOT NULL,
    unpriced_calls INTEGER NOT NULL,
    retrospective_calls INTEGER NOT NULL,
    uncached_nusd INTEGER NOT NULL,
    uncached_calls INTEGER NOT NULL,
    estimated_token_calls INTEGER NOT NULL,
    PRIMARY KEY (session_key, run_id, model_key)
) WITHOUT ROWID;
CREATE TABLE agg_usage (
    hour INTEGER NOT NULL,
    unit_key INTEGER NOT NULL,
    origin TEXT NOT NULL,
    model_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    calls INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    unreported_calls INTEGER NOT NULL,
    reported_nusd INTEGER NOT NULL,
    reported_calls INTEGER NOT NULL,
    estimated_nusd INTEGER NOT NULL,
    estimated_calls INTEGER NOT NULL,
    unpriced_calls INTEGER NOT NULL,
    retrospective_calls INTEGER NOT NULL,
    uncached_nusd INTEGER NOT NULL,
    uncached_calls INTEGER NOT NULL,
    estimated_token_calls INTEGER NOT NULL,
    PRIMARY KEY (hour, unit_key, origin, model_key, kind, status)
) WITHOUT ROWID;
CREATE INDEX agg_usage_unit ON agg_usage(unit_key);
CREATE TABLE agg_tools (
    hour INTEGER NOT NULL,
    session_key INTEGER NOT NULL,
    origin TEXT NOT NULL,
    name TEXT NOT NULL,
    calls INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    rejected INTEGER NOT NULL,
    unknown INTEGER NOT NULL,
    duration_calls INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    max_ms INTEGER,
    PRIMARY KEY (hour, session_key, origin, name)
) WITHOUT ROWID;
CREATE INDEX agg_tools_session ON agg_tools(session_key);
CREATE TABLE agg_tool_latency (
    hour INTEGER NOT NULL,
    session_key INTEGER NOT NULL,
    origin TEXT NOT NULL,
    name TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (hour, session_key, origin, name, bucket)
) WITHOUT ROWID;
CREATE INDEX agg_tool_latency_session ON agg_tool_latency(session_key);
CREATE TABLE agg_records (
    hour INTEGER NOT NULL,
    session_key INTEGER NOT NULL,
    role TEXT NOT NULL,
    records INTEGER NOT NULL,
    visible_steps INTEGER NOT NULL,
    PRIMARY KEY (hour, session_key, role)
) WITHOUT ROWID;
CREATE INDEX agg_records_session ON agg_records(session_key);
CREATE TABLE agg_cache (
    hour INTEGER NOT NULL,
    session_key INTEGER NOT NULL,
    turns INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    evaluated_turns INTEGER NOT NULL,
    suspected_turns INTEGER NOT NULL,
    last_instant INTEGER NOT NULL,
    PRIMARY KEY (hour, session_key)
) WITHOUT ROWID;
CREATE INDEX agg_cache_session ON agg_cache(session_key);
CREATE TABLE agg_cache_breaks (
    session_key INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    instant INTEGER NOT NULL,
    model_key TEXT NOT NULL,
    previous_input_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    PRIMARY KEY (session_key, seq)
) WITHOUT ROWID;
"""

ROLLUP_TABLES = (
    "agg_runs",
    "agg_run_models",
    "agg_usage",
    "agg_tools",
    "agg_tool_latency",
    "agg_records",
    "agg_cache",
    "agg_cache_breaks",
)
