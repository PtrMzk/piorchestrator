"""Tests for the orchestration loop — integration tests with mocks."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from po.db.connection import init_db
from po.db.queries import AgentResult, SqliteTaskStore
from po.orchestrator.loop import OrchestratorLoop
from po.spec.schema import ProjectSpec

from .conftest import MockAgentRunner, MockMergeStrategy, MockWorktreeProvider


@pytest.fixture
def orchestrator_env(tmp_path: Path, sample_spec: ProjectSpec):
    """Set up a full orchestrator environment with mocks."""
    project_root = tmp_path / "project"
    project_root.mkdir()

    db_path = project_root / ".po" / "state.db"
    conn = init_db(db_path)
    store = SqliteTaskStore(conn)
    store.save_spec(sample_spec)

    mock_agent = MockAgentRunner()
    mock_wt = MockWorktreeProvider(tmp_path / "worktrees")
    mock_merger = MockMergeStrategy()

    orchestrator = OrchestratorLoop(
        store=store,
        project_root=project_root,
        max_concurrency=2,
        worktree_manager=mock_wt,
        agent_runner=mock_agent,
        merger=mock_merger,
        max_retries=0,
    )

    return {
        "orchestrator": orchestrator,
        "store": store,
        "mock_agent": mock_agent,
        "mock_wt": mock_wt,
        "mock_merger": mock_merger,
        "conn": conn,
    }


class TestOrchestratorLoop:
    @pytest.mark.asyncio
    async def test_all_tasks_complete(self, orchestrator_env: dict) -> None:
        orch = orchestrator_env["orchestrator"]
        store = orchestrator_env["store"]

        await orch.run()

        tasks = store.get_all_tasks()
        for task in tasks:
            assert task["status"] == "completed", f"Task {task['id']} is {task['status']}"

    @pytest.mark.asyncio
    async def test_execution_order(self, orchestrator_env: dict) -> None:
        mock_agent: MockAgentRunner = orchestrator_env["mock_agent"]
        orch = orchestrator_env["orchestrator"]

        await orch.run()

        call_ids = [c["task_id"] for c in mock_agent.calls]
        # task-a must be first
        assert call_ids[0] == "task-a"
        # task-d must be after task-b and task-c
        assert call_ids.index("task-d") > call_ids.index("task-b")
        assert call_ids.index("task-d") > call_ids.index("task-c")

    @pytest.mark.asyncio
    async def test_all_tasks_merged(self, orchestrator_env: dict) -> None:
        mock_merger: MockMergeStrategy = orchestrator_env["mock_merger"]
        orch = orchestrator_env["orchestrator"]

        await orch.run()

        assert set(mock_merger.merged) == {"task-a", "task-b", "task-c", "task-d"}

    @pytest.mark.asyncio
    async def test_failed_task_cancels_dependents(self, orchestrator_env: dict) -> None:
        store: SqliteTaskStore = orchestrator_env["store"]
        mock_agent: MockAgentRunner = orchestrator_env["mock_agent"]
        orch: OrchestratorLoop = orchestrator_env["orchestrator"]

        # Configure task-a to fail
        mock_agent.outcomes["task-a"] = AgentResult(
            task_id="task-a",
            success=False,
            error_message="Simulated failure",
        )

        await orch.run()

        task_a = store.get_task("task-a")
        assert task_a is not None
        assert task_a["status"] == "failed"

        # All dependents should be cancelled
        for tid in ["task-b", "task-c", "task-d"]:
            task = store.get_task(tid)
            assert task is not None
            assert task["status"] == "cancelled", f"Task {tid} is {task['status']}"

    @pytest.mark.asyncio
    async def test_merge_failure_fails_task(self, orchestrator_env: dict) -> None:
        store: SqliteTaskStore = orchestrator_env["store"]
        mock_merger: MockMergeStrategy = orchestrator_env["mock_merger"]
        orch: OrchestratorLoop = orchestrator_env["orchestrator"]

        # Configure task-a merge to fail
        mock_merger.fail_tasks.add("task-a")

        await orch.run()

        task_a = store.get_task("task-a")
        assert task_a is not None
        assert task_a["status"] == "failed"

    @pytest.mark.asyncio
    async def test_merge_failure_retries_with_error_context(self, tmp_path: Path) -> None:
        """Merge failures should retry when max_retries > 0, passing error to agent."""
        spec_dict = {
            "project_name": "merge-retry-test",
            "tasks": [
                {"id": "t1", "description": "A task", "output_files": ["a.py"]},
            ],
        }
        spec = ProjectSpec.from_dict(spec_dict)
        project_root = tmp_path / "project"
        project_root.mkdir()

        db_path = project_root / ".po" / "state.db"
        conn = init_db(db_path)
        store = SqliteTaskStore(conn)
        store.save_spec(spec)

        mock_agent = MockAgentRunner()
        mock_merger = MockMergeStrategy()
        # Merge always fails for t1
        mock_merger.fail_tasks.add("t1")

        orch = OrchestratorLoop(
            store=store,
            project_root=project_root,
            max_concurrency=1,
            worktree_manager=MockWorktreeProvider(tmp_path / "wt"),
            agent_runner=mock_agent,
            merger=mock_merger,
            max_retries=1,
        )

        await orch.run()

        # Agent should have been called twice (initial + 1 retry)
        t1_calls = [c for c in mock_agent.calls if c["task_id"] == "t1"]
        assert len(t1_calls) == 2

        # Second call should contain the merge error in the prompt
        assert "Mock merge failure for t1" in t1_calls[1]["prompt"]
        assert "Previous Attempt Failed" in t1_calls[1]["prompt"]

        # After exhausting retries, task should be failed
        task = store.get_task("t1")
        assert task is not None
        assert task["status"] == "failed"

    @pytest.mark.asyncio
    async def test_merge_failure_succeeds_on_retry(self, tmp_path: Path) -> None:
        """Merge failure on first attempt, success on second."""
        spec_dict = {
            "project_name": "merge-retry-ok",
            "tasks": [
                {"id": "t1", "description": "A task", "output_files": ["a.py"]},
            ],
        }
        spec = ProjectSpec.from_dict(spec_dict)
        project_root = tmp_path / "project"
        project_root.mkdir()

        db_path = project_root / ".po" / "state.db"
        conn = init_db(db_path)
        store = SqliteTaskStore(conn)
        store.save_spec(spec)

        call_count = 0

        class FailOnceMerger(MockMergeStrategy):
            async def merge(self, branch, task_id, verification, project_root):
                from po.orchestrator.merge import MergeResult

                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return MergeResult(
                        success=False,
                        error_message="Verification failed: npx tsc prompt",
                    )
                self.merged.append(task_id)
                return MergeResult(success=True)

        orch = OrchestratorLoop(
            store=store,
            project_root=project_root,
            max_concurrency=1,
            worktree_manager=MockWorktreeProvider(tmp_path / "wt"),
            agent_runner=MockAgentRunner(),
            merger=FailOnceMerger(),
            max_retries=1,
        )

        await orch.run()

        task = store.get_task("t1")
        assert task is not None
        assert task["status"] == "completed"

    @pytest.mark.asyncio
    async def test_concurrency_respected(self, orchestrator_env: dict) -> None:
        """Verify that max_concurrency limits parallel agent runs."""
        # Track concurrent runs
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        original_runner = orchestrator_env["mock_agent"]

        class TrackingRunner:
            def __init__(self) -> None:
                self.calls = original_runner.calls

            async def run(self, **kwargs):
                nonlocal max_concurrent, current_concurrent
                async with lock:
                    current_concurrent += 1
                    max_concurrent = max(max_concurrent, current_concurrent)
                await asyncio.sleep(0.01)
                async with lock:
                    current_concurrent -= 1
                return await original_runner.run(**kwargs)

        orch: OrchestratorLoop = orchestrator_env["orchestrator"]
        orch.agent_runner = TrackingRunner()
        orch.max_concurrency = 2

        await orch.run()

        assert max_concurrent <= 2

    @pytest.mark.asyncio
    async def test_retry_terminates_on_pre_set_running_failure(self, tmp_path: Path) -> None:
        """Tasks that fail before set_running (e.g. worktree creation) must not retry forever."""
        spec_dict = {
            "project_name": "retry-test",
            "tasks": [
                {"id": "t1", "description": "A", "output_files": ["a.py"]},
            ],
        }
        spec = ProjectSpec.from_dict(spec_dict)
        project_root = tmp_path / "project"
        project_root.mkdir()

        db_path = project_root / ".po" / "state.db"
        conn = init_db(db_path)
        store = SqliteTaskStore(conn)
        store.save_spec(spec)

        class FailingWorktree(MockWorktreeProvider):
            def create(self, task_id, project_root):
                raise RuntimeError("Worktree creation failed")

        orch = OrchestratorLoop(
            store=store,
            project_root=project_root,
            max_concurrency=1,
            worktree_manager=FailingWorktree(tmp_path / "wt"),
            agent_runner=MockAgentRunner(),
            merger=MockMergeStrategy(),
            max_retries=1,
        )

        await orch.run()

        task = store.get_task("t1")
        assert task is not None
        assert task["status"] == "failed", f"Expected failed, got {task['status']}"
        # Should have attempted twice: initial + 1 retry
        assert task["attempt"] == 2

    @pytest.mark.asyncio
    async def test_output_overlap_serialization(self, tmp_path: Path) -> None:
        """Tasks with overlapping output_files should not run concurrently."""
        spec_dict = {
            "project_name": "overlap-test",
            "tasks": [
                {"id": "t1", "description": "A", "output_files": ["shared.py"]},
                {"id": "t2", "description": "B", "output_files": ["shared.py"]},
            ],
        }
        spec = ProjectSpec.from_dict(spec_dict)
        project_root = tmp_path / "project"
        project_root.mkdir()

        db_path = project_root / ".po" / "state.db"
        conn = init_db(db_path)
        store = SqliteTaskStore(conn)
        store.save_spec(spec)

        run_order: list[str] = []

        class OrderTracker:
            async def run(self, task_id: str, **kwargs):
                run_order.append(f"start:{task_id}")
                await asyncio.sleep(0.01)
                run_order.append(f"end:{task_id}")
                return AgentResult(task_id=task_id, success=True, cost_usd=0.01, duration_ms=10)

        orch = OrchestratorLoop(
            store=store,
            project_root=project_root,
            max_concurrency=5,
            worktree_manager=MockWorktreeProvider(tmp_path / "wt"),
            agent_runner=OrderTracker(),
            merger=MockMergeStrategy(),
            max_retries=0,
        )

        await orch.run()

        # One must fully complete before the other starts
        # Find indices
        starts = [i for i, x in enumerate(run_order) if x.startswith("start:")]
        ends = [i for i, x in enumerate(run_order) if x.startswith("end:")]
        # The second start must come after the first end
        assert starts[1] > ends[0]
