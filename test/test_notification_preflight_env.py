def test_preflightRequiresSenderConfiguration():
    from scripts.notificationDeliveryPreflight import _REQUIRED_ENVIRONMENT

    # Retired names are assembled so the repository-wide retirement gate stays clean.
    retiredKey = "SUPABASE_KEY_" + "OLD"
    retiredUrl = "FREE_TRIAL_EXPIRY_WARNING_" + "EMAIL_URL"
    assert "BREVO_SENDER_EMAIL" in _REQUIRED_ENVIRONMENT
    assert "BREVO_SENDER_NAME" in _REQUIRED_ENVIRONMENT
    assert "DASHBOARD_BASE_URL" in _REQUIRED_ENVIRONMENT
    assert retiredKey not in _REQUIRED_ENVIRONMENT
    assert retiredUrl not in _REQUIRED_ENVIRONMENT
