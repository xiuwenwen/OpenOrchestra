from __future__ import annotations

from harness.testing.evidence import CommandEvidence, TestRunEvidence
from harness.testing.runners.base import TestRunRequest
from harness.testing.runners.docker import DockerTestRunner


class SweBenchTestRunner:
    runtime = "swebench"

    def __init__(self, docker_runner: DockerTestRunner | None = None):
        self.docker_runner = docker_runner or DockerTestRunner()

    def run(self, request: TestRunRequest) -> TestRunEvidence:
        swebench = request.config.get("testing", {}).get("swebench", {})
        if not isinstance(swebench, dict) or not swebench.get("enabled", False):
            return TestRunEvidence(
                status="skipped",
                runtime=self.runtime,
                image=request.profile.image,
                project_type=request.profile.project_type,
                environment_status="skipped",
                build_status="skipped",
                test_status="skipped",
                failure_type="none",
                commands=(
                    CommandEvidence(
                        name="swebench_not_configured",
                        command="n/a",
                        exit_code=None,
                        stderr="SWE-bench runner is available but not configured for this Harness task.",
                    ),
                ),
                notes=("Configure testing.swebench.enabled and command integration to run official SWE-bench evaluation inside Harness.",),
            )
        return self.docker_runner.run(request)
