from ai_browser_agent.agent.subagents import ExecutorAgent, ExtractorAgent, SafetyReviewerAgent


def test_executor_agent_classifies_action_tools() -> None:
    executor = ExecutorAgent()

    assert executor.should_observe_after("click")
    assert executor.should_check_progress("navigate")
    assert not executor.should_observe_after("query_dom")


def test_extractor_agent_routes_long_reading_tasks() -> None:
    extractor = ExtractorAgent()

    assert extractor.should_use_extract("Summarize this page", visible_text_chars=100)
    assert extractor.should_use_extract("Inspect page", visible_text_chars=6000)
    assert not extractor.should_use_extract("Click the button", visible_text_chars=100)


def test_safety_reviewer_agent_flags_high_risk() -> None:
    reviewer = SafetyReviewerAgent()

    assert reviewer.needs_review("high")
    assert reviewer.needs_review("critical")
    assert not reviewer.needs_review("low")

