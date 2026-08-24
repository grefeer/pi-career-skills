"""Focused contract tests for the evidence-grounded career-planning adapter."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from pi_career_skills.business.career_planning import PreparationPlanOutput
from pi_career_skills.business.career_planning.career_planning import (
    BuildPreparationPlanInput,
    CareerPlanningError,
    CareerPreparationPlanOutput,
    PreparationSchedule,
    build_preparation_plan,
)
from pi_career_skills.context import ToolContext
from pi_career_skills.registry import CareerToolRegistry, ToolDefinition


def _context(metadata: dict[object, object]) -> ToolContext:
    return ToolContext(user_id="user", run_id="run", metadata=metadata)


def _planning_registry() -> CareerToolRegistry:
    """Register the adapter under test without changing the production catalog."""
    registry = CareerToolRegistry()
    registry.register(
        ToolDefinition(
            name="build-preparation-plan",
            skill_name="career-planning",
            description="test career-planning adapter",
            input_model=BuildPreparationPlanInput,
            output_model=CareerPreparationPlanOutput,
            handler=build_preparation_plan,
        )
    )
    return registry


def _evidence(
    *, artifact_id: str = "jd-1", text: str | None = None, title: str = "前端开发工程师"
) -> list[dict[str, str]]:
    return [
        {
            "artifact_id": artifact_id,
            "source_url": "https://jobs.example.test/role",
            "title": title,
            "visible_text": text
            or "前端开发工程师。要求熟悉 Vue3、TypeScript、Python 和 RAG。",
        }
    ]


def test_builds_jd_grounded_plan_with_normalized_topics() -> None:
    """Changing the JD intersection must change topics and resulting plan items."""
    result = build_preparation_plan(
        _context({"observed_public_evidence": _evidence(), "task_goal": "前端开发"}),
        BuildPreparationPlanInput(
            target_artifact_id="jd-1",
            focus_keywords=[" Vue3 ", "vue3", "TypeScript", "Kubernetes"],
            time_budget_hours=5,
        ),
    )

    assert result.target_artifact_id == "jd-1"
    assert result.resolved_target_artifact_id == "jd-1"
    assert result.source_url == "https://jobs.example.test/role"
    assert result.jd_topics == ["vue3", "typescript"]
    assert [item.topic for item in result.plan_items] == ["vue3", "typescript"]
    assert [item.time_budget_hours for item in result.plan_items] == [3, 2]
    assert result.actions


def test_limits_plan_items_to_available_hours_without_zero_hour_actions() -> None:
    """One hour over two matching topics must schedule only the stable first priority item."""
    result = build_preparation_plan(
        _context({"observed_public_evidence": _evidence(), "task_goal": "前端开发"}),
        BuildPreparationPlanInput(
            target_artifact_id="jd-1",
            focus_keywords=["Vue3", "TypeScript"],
            time_budget_hours=1,
        ),
    )

    assert result.jd_topics == ["vue3", "typescript"]
    assert [item.topic for item in result.plan_items] == ["vue3"]
    assert [item.time_budget_hours for item in result.plan_items] == [1]
    assert all(item.time_budget_hours > 0 for item in result.plan_items)
    assert all("TypeScript" not in action for action in result.actions)


def test_matches_english_focus_keywords_as_identifiers_not_substrings() -> None:
    """Changing JavaScript-only evidence must not manufacture a Java preparation item."""
    with pytest.raises(CareerPlanningError) as exc_info:
        build_preparation_plan(
            _context(
                {
                    "observed_public_evidence": _evidence(
                        text="前端开发工程师。要求熟悉 JavaScript 和 TypeScript。"
                    ),
                    "task_goal": "前端开发",
                }
            ),
            BuildPreparationPlanInput(target_artifact_id="jd-1", focus_keywords=["Java"]),
        )
    standalone_java = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": _evidence(
                    text="后端开发工程师。要求熟悉 Java、Spring Boot 和 MySQL。",
                    title="后端开发工程师",
                ),
                "task_goal": "",
            }
        ),
        BuildPreparationPlanInput(target_artifact_id="jd-1", focus_keywords=["Java"]),
    )

    assert exc_info.value.code == "focus_keywords_not_found"
    assert standalone_java.jd_topics == ["java"]
    assert standalone_java.plan_items[0].evidence_basis == "所选 JD 明确提及 Java。"


def test_package_exports_compatibility_output_alias() -> None:
    """Package-level source-compatible output import must resolve to the canonical model."""
    assert PreparationPlanOutput is CareerPreparationPlanOutput


def test_returns_resolved_canonical_artifact_id_not_candidate_selector() -> None:
    """A candidate-id selector must serialize the persisted artifact id for later audit."""
    result = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": [
                    {
                        "artifact_id": "page-artifact",
                        "source_url": "https://jobs.example.test/role",
                        "visible_text": "页面索引内容。",
                    }
                ],
                "structured_job_candidates": [
                    {
                        "candidate_id": "candidate-9",
                        "artifact_id": "canonical-jd-9",
                        "source_artifact_id": "page-artifact",
                        "source_url": "https://jobs.example.test/role",
                        "title": "前端开发工程师",
                        "full_text": "前端开发工程师。要求熟悉 Vue3。",
                    }
                ],
                "task_goal": "前端开发",
            }
        ),
        BuildPreparationPlanInput(target_artifact_id="candidate-9", focus_keywords=["Vue3"]),
    )

    assert result.target_artifact_id == "canonical-jd-9"
    assert result.resolved_target_artifact_id == "canonical-jd-9"
    assert result.selected_target_reference == "candidate-9"


def test_rejects_target_without_canonical_artifact_id() -> None:
    """A URL can select evidence but cannot become its canonical audit pointer."""
    source_url = "https://jobs.example.test/role-without-artifact"
    with pytest.raises(CareerPlanningError) as exc_info:
        build_preparation_plan(
            _context(
                {
                    "observed_public_evidence": [
                        {
                            "source_url": source_url,
                            "visible_text": "前端开发工程师。要求熟悉 Vue3。",
                        }
                    ],
                    "task_goal": "前端开发",
                }
            ),
            BuildPreparationPlanInput(target_artifact_id=source_url, focus_keywords=["Vue3"]),
        )
    assert exc_info.value.code == "target_evidence_incomplete"


@pytest.mark.parametrize(
    ("metadata", "params", "expected_code"),
    [
        (
            {"observed_public_evidence": []},
            {"target_artifact_id": "missing", "focus_keywords": ["Python"]},
            "target_evidence_not_found",
        ),
        (
            {"observed_public_evidence": _evidence(), "task_goal": "前端开发"},
            {"target_artifact_id": "jd-1", "focus_keywords": ["Kubernetes"]},
            "focus_keywords_not_found",
        ),
    ],
)
def test_adapter_preserves_stable_career_planning_error_codes(
    metadata: dict[object, object], params: dict[str, object], expected_code: str
) -> None:
    """Adapter invocation must not degrade deterministic planning errors to a generic code."""
    observation = _planning_registry().invoke(
        "build-preparation-plan", _context(metadata), params
    )

    assert observation.status == "failed"
    assert observation.error_code == expected_code


@pytest.mark.parametrize(
    ("metadata", "expected_code"),
    [
        ({"observed_public_evidence": []}, "target_evidence_not_found"),
        (
            {
                "observed_public_evidence": [
                    {"artifact_id": "jd-1", "source_url": "https://jobs.example.test/role"}
                ]
            },
            "target_evidence_incomplete",
        ),
    ],
)
def test_rejects_absent_or_incomplete_target_evidence(
    metadata: dict[object, object], expected_code: str
) -> None:
    """Removing a target or its JD text must block plan creation with a stable code."""
    with pytest.raises(CareerPlanningError) as exc_info:
        build_preparation_plan(
            _context(metadata),
            BuildPreparationPlanInput(target_artifact_id="jd-1", focus_keywords=["Vue3"]),
        )
    assert exc_info.value.code == expected_code


def test_rejects_target_for_a_role_that_conflicts_with_goal() -> None:
    """Replacing frontend JD evidence with a product role must reject the selected target."""
    with pytest.raises(CareerPlanningError) as exc_info:
        build_preparation_plan(
            _context(
                {
                    "observed_public_evidence": _evidence(
                        text="AIGC 产品经理，负责产品规划与需求分析。", title="AIGC 产品经理"
                    ),
                    "task_goal": "前端开发",
                }
            ),
            BuildPreparationPlanInput(target_artifact_id="jd-1", focus_keywords=["AIGC"]),
        )
    assert exc_info.value.code == "target_role_mismatch"


def test_uses_explicit_relative_schedule_without_target_date() -> None:
    """No supplied deadline must never produce a fabricated calendar date."""
    result = build_preparation_plan(
        _context({"observed_public_evidence": _evidence(), "task_goal": "前端开发"}),
        BuildPreparationPlanInput(target_artifact_id="jd-1", focus_keywords=["Vue3"]),
    )

    assert result.schedule.kind == "relative"
    assert result.schedule.target_date is None
    assert result.plan_items[0].due_date is None
    assert result.plan_items[0].relative_order == "first"
    assert "目标日期" in result.schedule_assumption


def test_preserves_user_supplied_target_date_exactly() -> None:
    """A supplied date is plan evidence and must remain unmodified in every plan item."""
    target_date = date(2030, 5, 6)
    result = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": _evidence(),
                "task_goal": "前端开发",
                "confirmed_target_date": "2030-05-06",
            }
        ),
        BuildPreparationPlanInput(
            target_artifact_id="jd-1", focus_keywords=["Vue3", "TypeScript"], target_date=target_date
        ),
    )

    assert result.schedule.kind == "target_date"
    assert result.schedule.target_date == target_date
    assert [item.due_date for item in result.plan_items] == [target_date, target_date]
    assert all(item.relative_order is None for item in result.plan_items)


def test_schedule_json_records_only_explicit_user_supplied_date_provenance() -> None:
    """Audit serialization must distinguish an explicit date from a relative schedule."""
    explicit = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": _evidence(),
                "task_goal": "前端开发",
                "confirmed_target_date": "2030-05-06",
            }
        ),
        BuildPreparationPlanInput(
            target_artifact_id="jd-1",
            focus_keywords=["Vue3"],
            target_date=date(2030, 5, 6),
        ),
    )
    relative = build_preparation_plan(
        _context({"observed_public_evidence": _evidence(), "task_goal": "前端开发"}),
        BuildPreparationPlanInput(target_artifact_id="jd-1", focus_keywords=["Vue3"]),
    )

    assert explicit.model_dump(mode="json")["schedule"] == {
        "kind": "target_date",
        "target_date": "2030-05-06",
        "relative_window": None,
        "target_date_provenance": "user_supplied",
    }
    assert relative.model_dump(mode="json")["schedule"] == {
        "kind": "relative",
        "target_date": None,
        "relative_window": "按 P0 后 P1 的相对顺序完成；具体日期待用户确认。",
        "target_date_provenance": None,
    }


@pytest.mark.parametrize("confirmed_target_date", [None, "2030-05-07", "not-a-date"])
def test_rejects_model_target_date_without_exact_confirmed_context(
    confirmed_target_date: str | None,
) -> None:
    """A model payload cannot assert a user date unless trusted context confirms it exactly."""
    metadata: dict[object, object] = {
        "observed_public_evidence": _evidence(),
        "task_goal": "前端开发",
    }
    if confirmed_target_date is not None:
        metadata["confirmed_target_date"] = confirmed_target_date
    with pytest.raises(CareerPlanningError) as exc_info:
        build_preparation_plan(
            _context(metadata),
            BuildPreparationPlanInput(
                target_artifact_id="jd-1",
                focus_keywords=["Vue3"],
                target_date=date(2030, 5, 6),
            ),
        )
    assert exc_info.value.code == "target_date_unconfirmed"


@pytest.mark.parametrize(
    "invalid_schedule",
    [
        {"kind": "relative"},
        {
            "kind": "relative",
            "target_date": date(2030, 5, 6),
            "relative_window": "按相对顺序完成。",
        },
        {
            "kind": "relative",
            "target_date_provenance": "user_supplied",
            "relative_window": "按相对顺序完成。",
        },
        {"kind": "target_date", "target_date": date(2030, 5, 6)},
        {
            "kind": "target_date",
            "target_date": date(2030, 5, 6),
            "target_date_provenance": "user_supplied",
            "relative_window": "不应同时设置。",
        },
    ],
)
def test_rejects_inconsistent_schedule_model_shapes(
    invalid_schedule: dict[str, object],
) -> None:
    """Schedule variants must not serialize contradictory deadline provenance."""
    with pytest.raises(ValidationError):
        PreparationSchedule(**invalid_schedule)


def test_optionally_aggregates_closed_set_skill_gaps() -> None:
    """Only confirmed skills are owned; payload claims cannot hide unconfirmed gaps."""
    result = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": [
                    *_evidence(text="前端开发工程师，要求 Python、RAG、TypeScript。"),
                    {
                        "artifact_id": "jd-2",
                        "source_url": "https://jobs.example.test/role-2",
                        "title": "前端开发工程师",
                        "visible_text": "前端开发工程师，要求 Python、RAG、Docker。",
                    },
                ],
                "confirmed_profile_facts": {
                    "skills": ["TypeScript"],
                    "name": "Python and RAG specialist",
                    "projects": ["Python and RAG implementation"],
                },
                "task_goal": "前端开发",
            }
        ),
        BuildPreparationPlanInput(
            target_artifact_id="jd-1",
            focus_keywords=["Python"],
            additional_target_artifact_ids=["jd-2"],
            resume_skills=["Python"],
            gap_limit=2,
        ),
    )

    assert [(gap.skill, gap.job_count) for gap in result.skill_gaps] == [
        ("Python", 2),
        ("RAG", 2),
    ]


def test_does_not_generate_skill_gaps_without_confirmed_profile_facts() -> None:
    """Unconfirmed resume input alone cannot establish that any skill is missing."""
    result = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": [
                    *_evidence(text="前端开发工程师，要求 Python、RAG。"),
                    {
                        "artifact_id": "jd-2",
                        "source_url": "https://jobs.example.test/role-2",
                        "title": "前端开发工程师",
                        "visible_text": "前端开发工程师，要求 Python、Docker。",
                    },
                ],
                "task_goal": "前端开发",
            }
        ),
        BuildPreparationPlanInput(
            target_artifact_id="jd-1",
            focus_keywords=["Python"],
            additional_target_artifact_ids=["jd-2"],
            resume_skills=["Python"],
        ),
    )

    assert result.skill_gaps == []


@pytest.mark.parametrize(
    "confirmed_profile_facts",
    [
        {"name": "Confirmed candidate"},
        {"skills": "Python"},
        {"skills": ["Python", 42]},
    ],
)
def test_requires_a_confirmed_list_of_string_skills_for_gap_calculation(
    confirmed_profile_facts: dict[object, object],
) -> None:
    """Names or malformed skills fields cannot authorize a deterministic skill-gap claim."""
    result = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": _evidence(text="前端开发工程师，要求 Python。"),
                "confirmed_profile_facts": confirmed_profile_facts,
                "task_goal": "前端开发",
            }
        ),
        BuildPreparationPlanInput(
            target_artifact_id="jd-1",
            focus_keywords=["Python"],
            additional_target_artifact_ids=["jd-1"],
        ),
    )

    assert result.skill_gaps == []


def test_empty_confirmed_skills_list_authorizes_unowned_skill_gaps() -> None:
    """An explicit empty skills list means the profile was confirmed with no owned skill tags."""
    result = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": _evidence(text="前端开发工程师，要求 Python。"),
                "confirmed_profile_facts": {"skills": []},
                "task_goal": "前端开发",
            }
        ),
        BuildPreparationPlanInput(
            target_artifact_id="jd-1",
            focus_keywords=["Python"],
            additional_target_artifact_ids=["jd-1"],
        ),
    )

    gap_counts = {gap.skill: gap.job_count for gap in result.skill_gaps}
    assert gap_counts["Python"] == 1


def test_normalizes_confirmed_skill_aliases_before_calculating_gaps() -> None:
    """The explicit confirmed skills list must honor reviewed aliases such as JS."""
    result = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": _evidence(
                    text="前端开发工程师，要求 JavaScript 和 TypeScript。"
                ),
                "confirmed_profile_facts": {"skills": ["JS"]},
                "task_goal": "前端开发",
            }
        ),
        BuildPreparationPlanInput(
            target_artifact_id="jd-1",
            focus_keywords=["JavaScript"],
            additional_target_artifact_ids=["jd-1"],
        ),
    )

    assert "JavaScript" not in {gap.skill for gap in result.skill_gaps}


def test_deduplicates_same_jd_selected_by_candidate_id_artifact_id_and_url() -> None:
    """Alias selectors for one resolved artifact must contribute its skills only once."""
    source_url = "https://jobs.example.test/canonical-role"
    result = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": [
                    {
                        "artifact_id": "page-artifact",
                        "source_url": source_url,
                        "title": "前端开发工程师",
                        "visible_text": "前端开发工程师。要求 Python、RAG。",
                    }
                ],
                "structured_job_candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "artifact_id": "canonical-jd-1",
                        "source_artifact_id": "page-artifact",
                        "source_url": source_url,
                        "title": "前端开发工程师",
                        "full_text": "前端开发工程师。要求 Python、RAG。",
                    }
                ],
                "confirmed_profile_facts": {"skills": []},
                "task_goal": "前端开发",
            }
        ),
        BuildPreparationPlanInput(
            target_artifact_id="candidate-1",
            focus_keywords=["Python"],
            additional_target_artifact_ids=[
                "canonical-jd-1",
                "page-artifact",
                source_url,
                "candidate-1",
            ],
        ),
    )

    gap_counts = {gap.skill: gap.job_count for gap in result.skill_gaps}
    assert gap_counts["Python"] == 1
    assert gap_counts["RAG"] == 1
    assert all(count == 1 for count in gap_counts.values())


def test_keeps_distinct_structured_candidates_from_one_raw_page() -> None:
    """Two candidates from one careers page are separate JDs, not URL aliases of each other."""
    source_url = "https://jobs.example.test/careers"
    result = build_preparation_plan(
        _context(
            {
                "observed_public_evidence": [
                    {
                        "artifact_id": "careers-page",
                        "source_url": source_url,
                        "title": "Engineering careers",
                        "visible_text": "招聘 Python 工程师和 Docker 平台工程师。",
                    }
                ],
                "structured_job_candidates": [
                    {
                        "candidate_id": "candidate-a",
                        "artifact_id": "canonical-python",
                        "source_artifact_id": "careers-page",
                        "source_url": source_url,
                        "title": "Python 工程师",
                        "full_text": "Python 工程师。要求 Python。",
                    },
                    {
                        "candidate_id": "candidate-b",
                        "artifact_id": "canonical-docker",
                        "source_artifact_id": "careers-page",
                        "source_url": source_url,
                        "title": "平台工程师",
                        "full_text": "平台工程师。要求 Docker。",
                    },
                ],
                "confirmed_profile_facts": {"skills": []},
            }
        ),
        BuildPreparationPlanInput(
            target_artifact_id="candidate-a",
            focus_keywords=["Python"],
            additional_target_artifact_ids=["candidate-b"],
        ),
    )

    gap_counts = {gap.skill: gap.job_count for gap in result.skill_gaps}
    assert gap_counts["Python"] == 1
    assert gap_counts["Docker"] == 1
