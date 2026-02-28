"""Tests for model escalation on retries."""

from __future__ import annotations

from pathlib import Path

import pytest

from po.config import escalate_model
from po.db.connection import init_db
from po.db.queries import SqliteTaskStore
from po.orchestrator.loop import OrchestratorLoop
from po.spec.schema import ProjectSpec

from .conftest import MockAgentRunner, MockMergeStrategy, MockWorktreeProvider


# --- escalate_model unit tests ---


class TestEscalateModel:
    """Tests for the escalate_model pure function."""

    def test_attempt_1_returns_base(self):
        assert escalate_model("haiku", 1) == "haiku"

    def test_attempt_2_escalates_haiku_to_sonnet(self):
        assert escalate_model("haiku", 2) == "sonnet"

    def test_attempt_3_escalates_haiku_to_opus(self):
        assert escalate_model("haiku", 3) == "opus"

    def test_attempt_4_clamps_to_opus(self):
        assert escalate_model("haiku", 4) == "opus"

    def test_sonnet_attempt_1(self):
        assert escalate_model("sonnet", 1) == "sonnet"

    def test_sonnet_attempt_2_escalates_to_opus(self):
        assert escalate_model("sonnet", 2) == "opus"

    def test_sonnet_attempt_3_clamps_to_opus(self):
        assert escalate_model("sonnet", 3) == "opus"

    def test_opus_stays_opus(self):
        assert escalate_model("opus", 1) == "opus"
        assert escalate_model("opus", 2) == "opus"
        assert escalate_model("opus", 3) == "opus"

    def test_unknown_model_returned_unchanged(self):
        assert escalate_model("gpt-4", 1) == "gpt-4"
        assert escalate_model("gpt-4", 2) == "gpt-4"
        assert escalate_model("gpt-4", 5) == "gpt-4"


# --- Integration tests ---


class TestEscalationIntegration:
    """Integration tests for model escalation during orchestration."""

    @pytest.mark.asyncio
    async def test_model_escalates_on_retry(self, tmp_path: Path) -> None:
        """After a merge failure, the retry should use an escalated model."""
        spec_dict = {
            "project_name": "escalation-test",
            "tasks": [
                {
                    "id": "t1",
                    "description": "A task",
                    "output_files": ["a.py"],
                    "model": "haiku",
                },
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
        merge_count = 0

        class FailOnceMerger(MockMergeStrategy):
            async def merge(self, branch, task_id, verification, project_root):
                from po.orchestrator.merge import MergeResult

                nonlocal merge_count
                merge_count += 1
                if merge_count == 1:
                    return MergeResult(
                        success=False,
                        error_message="Merge conflict",
                    )
                self.merged.append(task_id)
                return MergeResult(success=True)

        orch = OrchestratorLoop(
            store=store,
            project_root=project_root,
            max_concurrency=1,
            worktree_manager=MockWorktreeProvider(tmp_path / "wt"),
            agent_runner=mock_agent,
            merger=FailOnceMerger(),
            max_retries=1,
        )

        await orch.run()

        # Should be called twice
        t1_calls = [c for c in mock_agent.calls if c["task_id"] == "t1"]
        assert len(t1_calls) == 2
        # First call: haiku (attempt 1)
        assert t1_calls[0]["model"] == "haiku"
        # Second call: sonnet (attempt 2, escalated from haiku)
        assert t1_calls[1]["model"] == "sonnet"

    @pytest.mark.asyncio
    async def test_model_override_skips_escalation(self, tmp_path: Path) -> None:
        """When model_override is set, escalation should not apply."""
        spec_dict = {
            "project_name": "override-test",
            "tasks": [
                {
                    "id": "t1",
                    "description": "A task",
                    "output_files": ["a.py"],
                    "model": "haiku",
                },
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
        merge_count = 0

        class FailOnceMerger(MockMergeStrategy):
            async def merge(self, branch, task_id, verification, project_root):
                from po.orchestrator.merge import MergeResult

                nonlocal merge_count
                merge_count += 1
                if merge_count == 1:
                    return MergeResult(
                        success=False,
                        error_message="Merge conflict",
                    )
                self.merged.append(task_id)
                return MergeResult(success=True)

        orch = OrchestratorLoop(
            store=store,
            project_root=project_root,
            max_concurrency=1,
            worktree_manager=MockWorktreeProvider(tmp_path / "wt"),
            agent_runner=mock_agent,
            merger=FailOnceMerger(),
            max_retries=1,
            model_override="sonnet",  # User override
        )

        await orch.run()

        t1_calls = [c for c in mock_agent.calls if c["task_id"] == "t1"]
        assert len(t1_calls) == 2
        # Both calls should use the override model, not escalate
        assert t1_calls[0]["model"] == "sonnet"
        assert t1_calls[1]["model"] == "sonnet"

    @pytest.mark.asyncio
    async def test_model_escalated_event_emitted(self, tmp_path: Path) -> None:
        """A model_escalated event should be emitted when the model changes."""
        spec_dict = {
            "project_name": "event-test",
            "tasks": [
                {
                    "id": "t1",
                    "description": "A task",
                    "output_files": ["a.py"],
                    "model": "haiku",
                },
            ],
        }
        spec = ProjectSpec.from_dict(spec_dict)
        project_root = tmp_path / "project"
        project_root.mkdir()

        db_path = project_root / ".po" / "state.db"
        conn = init_db(db_path)
        store = SqliteTaskStore(conn)
        store.save_spec(spec)

        events: list[tuple[str, str, str]] = []

        def capture_event(event, task_id, detail):
            events.append((event, task_id, detail))

        merge_count = 0

        class FailOnceMerger(MockMergeStrategy):
            async def merge(self, branch, task_id, verification, project_root):
                from po.orchestrator.merge import MergeResult

                nonlocal merge_count
                merge_count += 1
                if merge_count == 1:
                    return MergeResult(
                        success=False,
                        error_message="Merge conflict",
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
            on_event=capture_event,
        )

        await orch.run()

        escalated = [e for e in events if e[0] == "model_escalated"]
        assert len(escalated) == 1
        assert escalated[0][1] == "t1"
        assert "haiku" in escalated[0][2]
        assert "sonnet" in escalated[0][2]

    @pytest.mark.asyncio
    async def test_no_escalation_event_on_first_attempt(self, tmp_path: Path) -> None:
        """No model_escalated event on the first attempt (no escalation needed)."""
        spec_dict = {
            "project_name": "no-escalation-test",
            "tasks": [
                {
                    "id": "t1",
                    "description": "A task",
                    "output_files": ["a.py"],
                    "model": "haiku",
                },
            ],
        }
        spec = ProjectSpec.from_dict(spec_dict)
        project_root = tmp_path / "project"
        project_root.mkdir()

        db_path = project_root / ".po" / "state.db"
        conn = init_db(db_path)
        store = SqliteTaskStore(conn)
        store.save_spec(spec)

        events: list[tuple[str, str, str]] = []

        def capture_event(event, task_id, detail):
            events.append((event, task_id, detail))

        orch = OrchestratorLoop(
            store=store,
            project_root=project_root,
            max_concurrency=1,
            worktree_manager=MockWorktreeProvider(tmp_path / "wt"),
            agent_runner=MockAgentRunner(),
            merger=MockMergeStrategy(),
            max_retries=0,
            on_event=capture_event,
        )

        await orch.run()

        escalated = [e for e in events if e[0] == "model_escalated"]
        assert len(escalated) == 0
