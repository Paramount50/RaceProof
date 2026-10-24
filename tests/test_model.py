from pathlib import Path
from types import SimpleNamespace

from raceproof.config import Settings
from raceproof.contracts import (
    FailureBundle,
    FileReplacement,
    InvariantResult,
    InvestigationArtifact,
    InvestigationContext,
    InvestigationDecision,
    PatchProposal,
    RunEvent,
    ScheduleResult,
    ScheduleStatus,
)
from raceproof.model import GeminiInvestigationAgent, GeminiRepairAgent
from raceproof.target import TargetRegistry, load_adapter
from raceproof.usage import UsageLimits, UsageTracker


def repair_bundle() -> FailureBundle:
    invariant = InvariantResult(
        name="exclusive",
        passed=False,
        assertion="count <= 1",
        evidence={"count": 2},
        explanation="two rows",
    )
    event = RunEvent(
        sequence=1,
        kind="sql",
        stage="execute",
        action="SELECT",
        detail={"large_trace_marker": "never-repeat-this" * 1000},
    )
    schedule = ScheduleResult(
        schedule_name="race",
        status=ScheduleStatus.COMPLETED,
        completed=True,
        exact=True,
        reproduced_failure=True,
        actor_outcomes={"A": "accepted", "B": "accepted"},
        events=[event],
        invariant=invariant,
        elapsed_ms=1,
    )
    artifact = InvestigationArtifact(
        target_id="demo",
        target_operation="demo:reserve",
        confirmed_vulnerability=True,
        relevant_source_spans=[],
        reproduced_schedule=schedule,
        sql_trace=[event],
        violated_invariant=invariant,
        final_database_state={"count": 2},
        hypothesized_root_cause="check then insert",
        execution_interpretation="both actors committed",
        evidence_references=["schedule:race"],
    )
    return FailureBundle(
        target_id="demo",
        target_operation="demo:reserve",
        source_files={
            "src/demo.py": "def reserve():\n    return False\n",
            "src/models.py": "class Resource:\n    pass\n",
        },
        schema_ddl="CREATE TABLE resources (id integer primary key);",
        findings=[],
        reproduced_schedule=schedule,
        business_invariant="one reservation",
        allowed_outcomes={"accepted": 1, "rejected": 1},
        editable_paths=["src/demo.py"],
        protected_paths=["tests"],
        investigation_artifact=artifact,
    )


def test_gemini_roles_have_separate_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.resolved_investigation_model == "gemini-3.5-flash-lite"
    assert settings.resolved_repair_model == "gemini-3.1-flash-lite"
    assert settings.fallback_model == "gemini-3.8-flash"
    assert settings.enable_model_fallback is False


def test_gemini_key_loads_from_dotenv_without_being_serialized(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=local-secret\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.gemini_api_key_value == "local-secret"
    assert "local-secret" not in str(settings.gemini_api_key)


def test_default_dotenv_path_is_project_relative_not_cwd() -> None:
    env_file = Settings.model_config["env_file"]
    assert env_file == Path(__file__).resolve().parents[1] / ".env"


def test_gemini_model_reports_missing_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    available, reason = GeminiRepairAgent("gemini-3.8-flash").available()
    assert not available
    assert "GEMINI_API_KEY or GOOGLE_API_KEY" in reason


def test_gemini_accepts_ai_studio_key_and_structured_output(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-sent")
    model = GeminiRepairAgent("gemini-3.8-flash")
    available, reason = model.available()
    assert available, reason

    from langchain_google_genai import ChatGoogleGenerativeAI

    chat = ChatGoogleGenerativeAI(model=model.model_name, temperature=0)
    structured = chat.with_structured_output(PatchProposal)
    assert structured is not None


def test_agents_have_distinct_prompts_structures_and_tool_permissions() -> None:
    investigator = GeminiInvestigationAgent("gemini-3.8-flash")
    repair = GeminiRepairAgent("gemini-3.8-flash")
    assert investigator.system_prompt != repair.system_prompt
    assert investigator.tool_names != repair.tool_names
    assert investigator.tool_names == {
        "load_investigation_evidence",
        "read_latest_experiment_evidence",
    }
    assert repair.tool_names == {"load_repair_workspace"}
    assert "patch" not in " ".join(investigator.tool_names)
    assert InvestigationDecision.model_fields["experiment_name"]


def test_investigation_agent_tools_are_bound_to_approved_target_evidence() -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    source_files = {
        path: (target.repository_root / path).read_text(encoding="utf-8")
        for path in target.config.operation.source_files
    }
    context = InvestigationContext(
        target_id=target.config.id,
        target_name=target.config.name,
        target_operation=target.config.operation.callable,
        source_files=source_files,
        schema_ddl=load_adapter(target).schema_ddl(),
        findings=[],
        transaction_boundaries=["one transaction per actor"],
        checkpoints=["after_read"],
        available_experiments=[
            target.config.reproduction_schedule,
            *target.config.investigation_schedules,
        ],
        invariant=target.config.invariant,
    )
    agent = GeminiInvestigationAgent("gemini-3.8-flash")
    tools = {item.name: item for item in agent._tools(context)}
    assert set(tools) == set(agent.tool_names)
    selected = tools["load_investigation_evidence"].invoke(
        {"experiment_name": target.config.reproduction_schedule.name}
    )
    assert selected["approved"] is True
    assert "purchase_inventory" in next(iter(selected["approved_source"].values()))
    assert selected["schema_ddl"]
    assert len(tools) == 2


def test_two_turn_tool_loop_counts_each_internal_model_request(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    target = TargetRegistry(root / "examples").get("inventory_oversell")
    context = InvestigationContext(
        target_id=target.config.id,
        target_name=target.config.name,
        target_operation=target.config.operation.callable,
        source_files={
            path: (target.repository_root / path).read_text(encoding="utf-8")
            for path in target.config.operation.source_files
        },
        schema_ddl=load_adapter(target).schema_ddl(),
        findings=[],
        transaction_boundaries=["one transaction per actor"],
        checkpoints=["after_read"],
        available_experiments=[target.config.reproduction_schedule],
        invariant=target.config.invariant,
    )
    decision = InvestigationDecision(
        suspected_source=target.config.operation.source_files[0],
        concurrency_pattern="read check write",
        reasoning_summary="Both actors can use one observed value.",
        required_interleaving=["A reads", "B reads"],
        expected_invariant_violation=target.config.invariant.description,
        experiment_name=target.config.reproduction_schedule.name,
        confidence=0.9,
        supporting_evidence=["static finding"],
    )

    class TwoTurnAgent:
        def invoke(self, payload, config):
            for callback in config["callbacks"]:
                for _ in range(2):
                    callback.on_chat_model_start({}, [[]])
                    callback.on_llm_end(
                        SimpleNamespace(
                            generations=[
                                [
                                    SimpleNamespace(
                                        message=SimpleNamespace(
                                            usage_metadata={
                                                "input_tokens": 10,
                                                "output_tokens": 5,
                                                "total_tokens": 15,
                                            }
                                        )
                                    )
                                ]
                            ]
                        )
                    )
            return {
                "messages": [
                    SimpleNamespace(
                        tool_calls=[
                            {
                                "name": "load_investigation_evidence",
                                "args": {
                                    "experiment_name": decision.experiment_name,
                                },
                            }
                        ]
                    )
                ],
                "structured_response": decision.model_dump(),
            }

    monkeypatch.setattr("raceproof.model.create_agent", lambda *args, **kwargs: TwoTurnAgent())
    tracker = UsageTracker(UsageLimits(14, 6, 8, 60_000))
    agent = GeminiInvestigationAgent("gemini-test", tracker, api_key="not-sent", max_retries=0)
    result = agent.investigate(context)
    assert result.experiment_name == target.config.reproduction_schedule.name
    snapshot = tracker.snapshot()
    assert snapshot.total_requests == 2
    assert [item.operation for item in snapshot.request_log] == [
        "plan_experiment",
        "plan_experiment",
    ]


def test_repair_context_excludes_repeated_event_and_sql_histories() -> None:
    context = GeminiRepairAgent._compact_context(repair_bundle())
    rendered = str(context)
    assert "never-repeat-this" not in rendered
    assert "events" not in context["reproduction"]
    assert context["editable_source"]["src/demo.py"].startswith("def reserve")
    assert context["related_source"]["src/models.py"].startswith("class Resource")


def test_repair_retry_context_excludes_prior_patch_payloads() -> None:
    bundle = repair_bundle()
    bundle.previous_attempts = [
        {
            "number": 1,
            "proposal": {"unified_diff": "do-not-repeat-patch" * 5_000},
            "failure_reason": "git apply failed at line 4",
            "patch_validation": {"errors": ["corrupt patch at line 4"]},
        }
    ]
    rendered = str(GeminiRepairAgent._compact_context(bundle))
    assert "do-not-repeat-patch" not in rendered
    assert "corrupt patch at line 4" in rendered
    assert len(rendered) < 5_000


def test_repair_generation_is_one_bounded_structured_request(monkeypatch) -> None:
    proposal = PatchProposal(
        diagnosis="check then insert",
        strategy="serialize on resource",
        assumptions=[],
        file_replacements=[
            FileReplacement(path="src/demo.py", content="def reserve():\n    return True\n")
        ],
        expected_sql_shape=["FOR UPDATE"],
        residual_risks=[],
    )

    class StructuredChat:
        def with_structured_output(self, schema):
            assert schema is PatchProposal
            return self

        def invoke(self, payload, config):
            assert len(payload) == 2
            callback = config["callbacks"][0]
            callback.on_chat_model_start({}, [[]])
            callback.on_llm_end(
                SimpleNamespace(
                    generations=[
                        [
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    usage_metadata={
                                        "input_tokens": 900,
                                        "output_tokens": 300,
                                        "total_tokens": 1200,
                                    }
                                )
                            )
                        ]
                    ]
                )
            )
            return proposal

    monkeypatch.setattr("raceproof.model._gemini", lambda *args, **kwargs: StructuredChat())
    tracker = UsageTracker(UsageLimits(10, 2, 8, 20_000, 5_000))
    agent = GeminiRepairAgent(
        "gemini-test",
        tracker,
        api_key="not-sent",
        max_retries=0,
        max_repair_context_tokens=3_000,
    )
    assert agent.propose(repair_bundle()).file_replacements
    record = tracker.snapshot().request_log[0]
    assert tracker.snapshot().total_requests == 1
    assert record.message_count == 2
    assert record.estimated_input_tokens is not None
    assert record.estimated_input_tokens < 3_000
