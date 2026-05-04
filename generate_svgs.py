import urllib.request
import urllib.parse
import sys

def generate_svg(mermaid_text, output_file):
    url = 'https://kroki.io/mermaid/svg'
    data = mermaid_text.encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'text/plain', 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'})
    try:
        with urllib.request.urlopen(req) as response:
            svg = response.read()
            with open(output_file, 'wb') as f:
                f.write(svg)
            print(f"Successfully generated {output_file}")
    except Exception as e:
        print(f"Failed to generate {output_file}: {e}", file=sys.stderr)

architecture_mermaid = """
graph TD
    User((User)) --> |Prompt / Command| CLI[Interactive CLI / main.py]
    CLI --> |Classify Prompt| WC[Workflow Classifier]
    CLI --> |Create Task| ORCH[Orchestrator]
    subgraph "Core Harness Engine"
        ORCH
        WC
        VAL[Artifact Validator]
        JUDGE[Judge Runner]
        COMM[Communicator]
    end
    subgraph "Resource Managers"
        WM[Workspace Manager]
        AM[Artifact Manager]
        SR[State Repository]
    end
    ORCH <--> |Create isolated dirs| WM
    ORCH <--> |Validate & Hash| AM
    ORCH <--> |Persist DAG State| SR
    AM <--> |Read/Write| SR
    subgraph "Agent Adapters"
        AA_BASE{AgentAdapter}
        AA_CLAUDE[ClaudeCodeAdapter]
        AA_CODEX[CodexCLIAdapter]
        AA_MOCK[MockAgentAdapter]
        AA_BASE <|-- AA_CLAUDE
        AA_BASE <|-- AA_CODEX
        AA_BASE <|-- AA_MOCK
    end
    ORCH --> |Run Context| AA_BASE
    subgraph "Local File System"
        DB[(harness.db SQLite)]
        FS_WS[workspaces/ ]
        FS_ART[artifacts/ ]
        FS_DEL[deliver/ ]
    end
    SR --> DB
    WM --> FS_WS
    AM --> FS_ART
    COMM --> FS_DEL
    AA_BASE --> |Subprocess execution| FS_WS
    classDef core fill:#f9f,stroke:#333,stroke-width:2px;
    classDef manager fill:#bbf,stroke:#333,stroke-width:1px;
    classDef storage fill:#ddd,stroke:#333,stroke-width:1px;
    class ORCH,WC,VAL,JUDGE,COMM core;
    class WM,AM,SR manager;
    class DB,FS_WS,FS_ART,FS_DEL storage;
"""

workflow_mermaid = """
stateDiagram-v2
    direction TB
    [*] --> CREATED: User Prompt
    state "Planning Block" as PB {
        CREATED --> PLANNING_DRAFT: Planner Agents
        PLANNING_DRAFT --> PLAN_JUDGEMENT: Judge evaluates plan.md
        PLAN_JUDGEMENT --> PLANNING_DRAFT: Rejected (Changes Required)
    }
    state "Execution & Testing Loop" as ETL {
        PLAN_JUDGEMENT --> EXECUTION: Approved
        EXECUTION --> PATCH_MERGE: Executor Agents output patch.diff
        PATCH_MERGE --> TESTING: Merge 1 optimal patch
        TESTING --> TEST_JUDGEMENT: Tester outputs reports
        TEST_JUDGEMENT --> FIXING: Failed (Bugs found)
        FIXING --> PATCH_MERGE: Fix loop
    }
    state "Review Loop" as RL {
        TEST_JUDGEMENT --> REVIEWING: Passed (Tests OK)
        REVIEWING --> REVIEW_JUDGEMENT: Reviewer evaluates code quality
        REVIEW_JUDGEMENT --> REVIEW_FIXING: Rejected (Code smells/Regressions)
        REVIEW_FIXING --> PATCH_MERGE_2: Update implementation
        PATCH_MERGE_2 --> REGRESSION_TESTING
        REGRESSION_TESTING --> TEST_JUDGEMENT_2
        TEST_JUDGEMENT_2 --> REVIEW_FIXING: Regression Failed
        TEST_JUDGEMENT_2 --> REVIEWING: Regression Passed
    }
    state "Delivery Block" as DB {
        REVIEW_JUDGEMENT --> FINAL_JUDGEMENT: Review Approved
        FINAL_JUDGEMENT --> DELIVERY: Final Sanity Check Passed
        DELIVERY --> COMPLETED: Communicator generates final_delivery.md
    }
    COMPLETED --> [*]
"""

generate_svg(architecture_mermaid, "system_architecture.svg")
generate_svg(workflow_mermaid, "workflow_lifecycle.svg")
