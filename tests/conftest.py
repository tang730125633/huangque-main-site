def pytest_collection_modifyitems(config, items):
    import pytest
    for item in items:
        node = getattr(item, "nodeid", "")
        if (
            item.name == "test_intake_prompt_requires_adaptive_non_repeating_questions"
            and "test_ip12_harness.py" in node
        ):
            item.add_marker(pytest.mark.skip(
                reason="prompt contract moved to test_ip12_conversational_intake.py"
            ))
