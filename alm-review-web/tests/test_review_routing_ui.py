from app.web import templates


def test_configuration_exposes_skill_and_capability_controls() -> None:
    template = templates.get_template("configuration.html")
    source, _, _ = templates.env.loader.get_source(
        templates.env,
        template.name,
    )

    assert 'name="screenshot_terms"' not in source
    assert 'name="screenshot_exclusion_terms"' not in source
    assert 'name="ai_intent_classification_enabled"' not in source
    assert 'name="evidence_intent_skill_shadow_enabled"' not in source
    assert 'name="evidence_intent_confidence_threshold"' not in source
    assert 'name="specialist_reviews_enabled"' not in source
    assert "Enable downstream specialist reviews" not in source
    assert "Evidence candidate detection" not in source
    assert 'name="external_evidence_review_enabled"' in source
    assert "Enable external evidence review" in source
    assert "Directly controls approved HTML parsing" in source
    assert "Directly controls registry, calibration" in source
    assert "Read approved network evidence" not in source
    assert "Send evidence images to AI" not in source
    assert "Allow image transfer over HTTP" not in source
    assert "Enforced safety boundaries" in source
    assert "Enable AI Review processing" in source
    assert "It does not queue Reviews by itself" in source
    assert "Review processing &amp; Skills" in source
    assert "Global processing" in source
    assert "Workspace queue" in source
    assert "Skill capabilities" in source
    assert "Skill catalog" in source
    assert "skill_catalog" in source
    assert "required_capabilities" in source
    assert "skill.status == 'planned'" in source


def test_run_detail_exposes_image_stage_and_evidence_routing_trace() -> None:
    template = templates.get_template("run_detail.html")
    source, _, _ = templates.env.loader.get_source(
        templates.env,
        template.name,
    )

    assert "('image_review', 'Image review')" in source
    assert "Evidence routing trace" in source
    assert "Matched conditions" in source
    assert "route.get('actions', [])" in source
    assert "stage.get('steps')|length" in source
    assert "Evidence intent Skill" not in source
    assert "skill_shadow" not in source
    assert "Skill execution trace" in source
    assert "review_skill_traces" in source
    assert "trace.get('capabilities', {}).get('granted', [])" in source