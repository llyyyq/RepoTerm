from repoterm.runtime.planning.intent_parser import ActionType, IntentType, ParsedIntent
from repoterm.runtime.control.pipeline_engine import Step, StepExecutor, StepType
from repoterm.runtime.planning.task_object import ConstraintType, TaskObject
from repoterm.runtime.control.verification_controller import (
    VerificationController,
    VerificationMode,
    VerificationRisk,
    VerificationSignal,
)


def _task(
    *,
    intent_type: IntentType = IntentType.CODE,
    action_type: ActionType = ActionType.UPDATE,
    files: list[str] | None = None,
) -> TaskObject:
    task = TaskObject(
        raw_input="update code",
        parsed_intent=ParsedIntent(
            raw_input="update code",
            intent_type=intent_type,
            action_type=action_type,
            confidence=1.0,
        ),
        relevant_files=files or [],
    )
    if intent_type in {IntentType.CODE, IntentType.DEBUG, IntentType.REFACTOR, IntentType.TEST}:
        task.add_constraint(ConstraintType.TEST_REQUIRED, reason="Code modification requires tests")
    return task


class TestVerificationController:
    def test_read_only_task_uses_no_verification(self):
        controller = VerificationController()
        plan = controller.plan(
            VerificationSignal(intent_type="question", action_type="read", changed_files=[])
        )
        assert plan.risk == VerificationRisk.LOW
        assert plan.mode == VerificationMode.NONE
        assert plan.commands == []

    def test_python_core_change_selects_targeted_tests(self):
        controller = VerificationController()
        plan = controller.plan(
            VerificationSignal(
                changed_files=["repoterm/context/compactor.py"],
                intent_type="code",
                action_type="update",
                requires_tests=True,
            )
        )
        assert plan.risk in {VerificationRisk.HIGH, VerificationRisk.CRITICAL}
        assert plan.mode in {VerificationMode.TARGETED, VerificationMode.FULL}
        assert any(
            "test_context_cybernetics.py" in cmd or cmd == "pytest -q"
            for cmd in plan.commands
        )
        assert any("test_context_compactor.py" in cmd for cmd in plan.commands)
        assert any("test_cost_control.py" in cmd for cmd in plan.commands)
        assert not any("tests/test_compactor.py" in cmd for cmd in plan.commands)

    def test_tool_package_paths_map_to_existing_test_targets(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            ["repoterm/tools/registry.py", "repoterm/tools/background.py"]
        )

        assert targets == ["tests/test_tools.py", "tests/test_background_tasks.py"]
        assert "tests/test_registry.py" not in targets
        assert "tests/test_background.py" not in targets

    def test_session_service_path_maps_to_session_test_target(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(["repoterm/session/service.py"])

        assert targets == ["tests/test_session.py"]
        assert "tests/test_service.py" not in targets

    def test_app_entry_paths_map_to_existing_behavior_and_contract_tests(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            [
                "repoterm/app/interactive.py",
                "repoterm/app/headless.py",
                "repoterm/app/readiness.py",
                "repoterm/app/structure_check.py",
                "repoterm/app/engineering_structure.py",
                "repoterm/app/install.py",
            ]
        )

        assert targets == [
            "tests/test_main.py",
            "tests/contracts/test_app_package_contract.py",
            "tests/test_headless.py",
            "tests/test_product_surfaces.py",
            "tests/test_engineering_structure.py",
        ]
        assert "tests/test_interactive.py" not in targets
        assert "tests/test_readiness.py" not in targets
        assert "tests/test_install.py" not in targets
        assert "tests/test_structure_check.py" not in targets
        assert targets.count("tests/contracts/test_app_package_contract.py") == 1

    def test_evaluation_paths_map_to_explicit_tests_and_contract(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            [
                "benchmarks/evaluation/llm_e2e.py",
                "benchmarks/evaluation/runtime_profile.py",
                "benchmarks/llm_e2e_eval.py",
                "benchmarks/runtime_profile_eval.py",
                "repoterm/providers/fallback_simulation.py",
            ]
        )

        assert targets == [
            "tests/test_llm_e2e_eval.py",
            "tests/contracts/test_evaluation_package_contract.py",
            "tests/test_runtime_profile_eval.py",
            "tests/test_runtime_profile_benchmark.py",
            "tests/test_fallback_simulation.py",
        ]
        assert "tests/test_llm_e2e.py" not in targets
        assert "tests/test_runtime_profile.py" not in targets
        assert targets.count("tests/contracts/test_evaluation_package_contract.py") == 1

    def test_contract_paths_map_to_runtime_and_contract_tests(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            [
                "repoterm/contracts/types.py",
                "repoterm/contracts/state.py",
                "repoterm/contracts/__init__.py",
            ]
        )

        assert targets == [
            "tests/contracts/test_runtime_contract.py",
            "tests/contracts/test_contracts_package_contract.py",
            "tests/test_agent_loop.py",
        ]
        assert targets.count("tests/contracts/test_contracts_package_contract.py") == 1

    def test_ui_root_paths_map_to_behavior_and_ui_contract_tests(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            [
                "repoterm/ui/commands.py",
                "repoterm/ui/tty.py",
                "repoterm/ui/history.py",
                "repoterm/ui/shortcuts.py",
                "repoterm/ui/manage.py",
            ]
        )

        assert targets == [
            "tests/test_cli_commands.py",
            "tests/contracts/test_ui_package_contract.py",
            "tests/test_tty_app.py",
            "tests/test_tui.py",
            "tests/test_main.py",
        ]
        assert "tests/test_commands.py" not in targets
        assert "tests/test_tty.py" not in targets
        assert targets.count("tests/contracts/test_ui_package_contract.py") == 1

    def test_ui_tui_paths_keep_tui_target_and_specialized_targets(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            [
                "repoterm/ui/tui/renderer.py",
                "repoterm/ui/tui/transcript.py",
                "repoterm/ui/tui/input_parser.py",
            ]
        )

        assert targets == [
            "tests/test_tui.py",
            "tests/contracts/test_ui_package_contract.py",
            "tests/test_renderer_performance.py",
            "tests/test_transcript_layout.py",
            "tests/test_property_based.py",
        ]
        assert "tests/test_renderer.py" not in targets
        assert "tests/test_transcript.py" not in targets
        assert "tests/test_input_parser.py" not in targets

    def test_legacy_user_profile_path_maps_to_cli_and_contract_tests(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            ["repoterm/memory/legacy_user_profile.py"]
        )

        assert targets == [
            "tests/test_cli_commands.py",
            "tests/contracts/test_legacy_user_profile_package_contract.py",
        ]

    def test_runtime_planning_paths_map_to_existing_behavior_and_contract_tests(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            [
                "repoterm/runtime/planning/intelligence.py",
                "repoterm/runtime/planning/router.py",
                "repoterm/runtime/planning/smart_router.py",
                "repoterm/runtime/planning/domain_classifier.py",
                "repoterm/runtime/planning/intent_parser.py",
                "repoterm/runtime/planning/task_graph.py",
                "repoterm/runtime/planning/task_object.py",
                "repoterm/runtime/planning/task_tracker.py",
                "repoterm/runtime/planning/capability_registry.py",
                "repoterm/runtime/planning/prompt.py",
                "repoterm/runtime/planning/prompt_pipeline.py",
            ]
        )

        assert targets == [
            "tests/test_agent_intelligence.py",
            "tests/contracts/test_runtime_planning_package_contract.py",
            "tests/test_cybernetic_orchestrator.py",
            "tests/test_agent_flow.py",
            "tests/test_progress_controller.py",
            "tests/test_turn_kernel.py",
            "tests/test_verification_controller.py",
            "tests/test_new_features.py",
            "tests/test_agent_loop.py",
            "tests/test_prompt.py",
        ]
        assert "tests/test_intelligence.py" not in targets
        assert "tests/test_router.py" not in targets
        assert "tests/test_task_object.py" not in targets
        assert "tests/test_prompt_pipeline.py" not in targets

    def test_runtime_control_paths_map_to_existing_behavior_and_contract_tests(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            [
                "repoterm/runtime/control/adaptive_pid_tuner.py",
                "repoterm/runtime/control/context_cybernetics.py",
                "repoterm/runtime/control/cost_control.py",
                "repoterm/runtime/control/cybernetic_ablation.py",
                "repoterm/runtime/control/cybernetic_orchestrator.py",
                "repoterm/runtime/control/cybernetic_supervisor.py",
                "repoterm/runtime/control/decoupling_controller.py",
                "repoterm/runtime/control/feedback_controller.py",
                "repoterm/runtime/control/feedforward_controller.py",
                "repoterm/runtime/control/pipeline_engine.py",
                "repoterm/runtime/control/predictive_controller.py",
                "repoterm/runtime/control/progress_controller.py",
                "repoterm/runtime/control/self_healing_engine.py",
                "repoterm/runtime/control/stability_monitor.py",
                "repoterm/runtime/control/state_observer.py",
                "repoterm/runtime/control/verification_controller.py",
            ]
        )

        assert targets == [
            "tests/test_advanced_cybernetics.py",
            "tests/contracts/test_runtime_control_package_contract.py",
            "tests/test_context_cybernetics.py",
            "tests/test_context_compactor.py",
            "tests/test_cost_control.py",
            "tests/test_cybernetic_ablation.py",
            "tests/test_cybernetic_orchestrator.py",
            "tests/test_cybernetic_supervisor.py",
            "tests/test_feedback_controller.py",
            "tests/test_feedforward_controller.py",
            "tests/test_progress_controller.py",
            "tests/test_verification_controller.py",
        ]
        assert "tests/test_adaptive_pid_tuner.py" not in targets
        assert "tests/test_pipeline_engine.py" not in targets
        assert targets.count("tests/contracts/test_runtime_control_package_contract.py") == 1

    def test_runtime_core_paths_map_to_existing_behavior_and_contract_tests(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            [
                "repoterm/runtime/turn_kernel.py",
                "repoterm/runtime/runtime_profiles.py",
            ]
        )

        assert targets == [
            "tests/test_turn_kernel.py",
            "tests/contracts/test_runtime_core_package_contract.py",
            "tests/test_runtime_profiles.py",
        ]
        assert "tests/test_runtime.py" not in targets

    def test_all_runtime_core_paths_keep_stable_behavior_target_mapping(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            [
                "repoterm/runtime/turn_kernel.py",
                "repoterm/runtime/runtime_profiles.py",
                "repoterm/runtime/auto_mode.py",
                "repoterm/runtime/hooks.py",
                "repoterm/runtime/reflection.py",
            ]
        )

        assert targets == [
            "tests/test_turn_kernel.py",
            "tests/contracts/test_runtime_core_package_contract.py",
            "tests/test_runtime_profiles.py",
            "tests/test_permissions.py",
            "tests/test_hooks.py",
            "tests/memory/test_runtime_memory_integration.py",
        ]
        assert "tests/test_auto_mode.py" not in targets
        assert "tests/test_reflection.py" not in targets

    def test_runtime_loop_path_maps_to_agent_loop_and_both_contracts(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(["repoterm/runtime/loop.py"])

        assert targets == [
            "tests/test_agent_loop.py",
            "tests/contracts/test_runtime_loop_package_contract.py",
            "tests/contracts/test_runtime_contract.py",
        ]
        assert "tests/test_loop.py" not in targets

    def test_runtime_surfaces_path_maps_to_product_surface_and_contract(self):
        controller = VerificationController()
        targets = controller._infer_test_targets(
            ["repoterm/runtime/surfaces.py"]
        )

        assert targets == [
            "tests/test_product_surfaces.py",
            "tests/contracts/test_runtime_surfaces_package_contract.py",
        ]
        assert "tests/test_surfaces.py" not in targets

    def test_previous_failure_escalates_to_full_verification(self):
        controller = VerificationController()
        plan = controller.plan(
            VerificationSignal(
                changed_files=["repoterm/runtime/loop.py", "tests/test_agent_loop.py"],
                intent_type="debug",
                action_type="update",
                requires_tests=True,
                previous_verification_failed=True,
                recent_failures=3,
            )
        )
        assert plan.risk == VerificationRisk.CRITICAL
        assert plan.mode == VerificationMode.FULL
        assert plan.commands == ["pytest -q"]

    def test_documentation_change_does_not_force_tests(self):
        controller = VerificationController()
        plan = controller.plan(
            VerificationSignal(
                changed_files=["docs/architecture.md"],
                intent_type="document",
                action_type="update",
            )
        )
        assert plan.mode == VerificationMode.NONE
        assert plan.should_run is False

    def test_feedback_signal_marks_failed_plan_for_escalation(self):
        controller = VerificationController()
        plan = controller.plan(
            VerificationSignal(changed_files=["repoterm/providers/registry.py"], intent_type="code")
        )
        feedback = controller.update_from_result(plan, passed=False)
        assert feedback.previous_verification_failed is True
        assert feedback.recent_failures == 1
        assert feedback.changed_files == plan.changed_files


class TestVerificationPipelineIntegration:
    def test_pipeline_verify_step_returns_risk_adaptive_plan(self):
        executor = StepExecutor()
        task = _task(files=["repoterm/runtime/control/context_cybernetics.py"])
        step = Step(
            id="verify",
            type=StepType.VERIFY,
            description="Verify correctness with tests",
            handler="run_tests",
        )

        success, result = executor.execute(step, task)

        assert success is True
        assert result["tests_passed"] is None
        assert result["verification_plan"]["mode"] in {"targeted", "full"}
        assert any("pytest" in cmd for cmd in result["verification_plan"]["commands"])
