CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    user_prompt TEXT NOT NULL,
    workflow_type TEXT,
    status TEXT NOT NULL,
    current_phase TEXT,
    current_role TEXT,
    configuration TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS phases (
    phase_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    phase_type TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    round_id INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    phase_id TEXT NOT NULL,
    role TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    retry_count INTEGER DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    phase_id TEXT,
    role TEXT,
    agent_id TEXT,
    artifact_type TEXT NOT NULL,
    version INTEGER NOT NULL,
    path TEXT NOT NULL,
    hash TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS judge_decisions (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    phase_id TEXT,
    decision_type TEXT NOT NULL,
    decision_payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

