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
    loop_type TEXT,
    parent_round_id INTEGER,
    iteration_id INTEGER,
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

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT,
    phase TEXT,
    role TEXT,
    agent_id TEXT,
    round_id INTEGER,
    attempt INTEGER,
    event_type TEXT NOT NULL,
    status TEXT,
    message TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_phases_task_id ON phases(task_id);
CREATE INDEX IF NOT EXISTS idx_phases_task_round ON phases(task_id, round_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_task_id ON agent_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_phase_id ON agent_runs(phase_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_task_id ON artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_phase_id ON artifacts(phase_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_task_type ON artifacts(task_id, artifact_type);
CREATE INDEX IF NOT EXISTS idx_artifacts_created_at ON artifacts(created_at);
CREATE INDEX IF NOT EXISTS idx_judge_decisions_task_id ON judge_decisions(task_id);
CREATE INDEX IF NOT EXISTS idx_judge_decisions_phase_id ON judge_decisions(phase_id);
CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
