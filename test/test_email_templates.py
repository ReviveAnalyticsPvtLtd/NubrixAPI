import datetime
import re

import pytest

from api.services.notifications.emailTemplates import (
    BILLING_TYPES,
    RenderedEmail,
    renderBillingMessage,
    renderFreeTrialWelcome,
    renderTrialExpiryWarning,
)

NAME = "<script>alert('x')</script>"
PERIOD_END = "2026-11-06T12:00:00+00:00"
APP = "https://app.example.test"


def render(notificationType, metadata=None, appUrl=APP, name=NAME):
    return renderBillingMessage(
        notificationType=notificationType,
        name=name,
        metadata=metadata or {},
        appUrl=appUrl,
    )


def assertRejected(code, **kwargs):
    with pytest.raises(ValueError) as error:
        renderBillingMessage(**kwargs)
    assert str(error.value) == code


def test_billing_types_follow_contract_order():
    assert BILLING_TYPES == (
        "annual_renewal_upcoming",
        "monthly_renewal_ready",
        "monthly_renewal_reminder",
        "monthly_subscription_expired",
        "payment_receipt",
        "monthly_cancellation_confirmation",
        "subscription_refund_initiated",
        "subscription_refund_processed",
        "payment_refund_started",
        "payment_refund_completed",
    )


def test_annual_milestones_describe_pro_with_yearly_coverage():
    upcoming = render(
        "annual_renewal_upcoming",
        {"billingInterval": "year", "invoiceId": "annual-invoice"},
    )
    assert isinstance(upcoming, RenderedEmail)
    assert "Pro" in upcoming.subject
    assert "annual-invoice" in upcoming.htmlContent
    ready = render("monthly_renewal_ready", {"billingInterval": "year"})
    assert "monthly" not in ready.subject
    assert "next calendar month" not in ready.textContent


def test_annual_interval_overrides_subjects():
    subjects = {
        notificationType: render(notificationType, {"billingInterval": "year"}).subject
        for notificationType in (
            "monthly_renewal_ready",
            "monthly_renewal_reminder",
            "monthly_subscription_expired",
            "monthly_cancellation_confirmation",
        )
    }
    assert subjects == {
        "monthly_renewal_ready": "Your Pro yearly renewal invoice is ready",
        "monthly_renewal_reminder": "Your Pro yearly coverage ends tomorrow",
        "monthly_subscription_expired": "Your Pro yearly coverage has expired",
        "monthly_cancellation_confirmation": "Pro renewal reminders stopped",
    }


def test_all_billing_types_render_their_own_escaped_message():
    subjects = set()
    for notificationType in BILLING_TYPES:
        message = render(
            notificationType,
            {"amount": 3000, "currency": "INR", "startedAt": PERIOD_END},
            appUrl="https://app.example.test/account",
        )
        assert "<script>" not in message.htmlContent
        assert "&lt;script&gt;" in message.htmlContent
        subjects.add(message.subject)
    assert len(subjects) == len(BILLING_TYPES)


def test_invalid_notification_type_or_recipient_name_cannot_be_rendered():
    assertRejected(
        "UNSUPPORTED_NOTIFICATION_TYPE",
        notificationType="trial_expiry_warning", name="A", metadata={}, appUrl=APP,
    )
    assertRejected(
        "UNSUPPORTED_NOTIFICATION_TYPE",
        notificationType="nope", name="A", metadata={}, appUrl=APP,
    )
    for name in ("", "   ", None):
        assertRejected(
            "INVALID_RECIPIENT_NAME",
            notificationType="payment_receipt", name=name, metadata={}, appUrl=APP,
        )


def test_unsafe_checkout_url_cannot_be_rendered():
    for appUrl in ("javascript:alert(1)", "", "not a url", "ftp://app.example.test",
                   "https://user:pw@app.example.test"):
        assertRejected(
            "INVALID_APP_URL",
            notificationType="payment_receipt", name="A", metadata={}, appUrl=appUrl,
        )


def test_renewal_invoice_contains_frozen_amount_experts_and_service_dates():
    message = render(
        "monthly_renewal_ready",
        {
            "amount": 3000, "currency": "INR", "domains": ["banking"],
            "invoiceId": "invoice-1",
            "nextStart": "2026-11-06T12:00:00+00:00",
            "nextEnd": "2026-12-06T12:00:00+00:00",
            "renewalDeadline": PERIOD_END,
            "checkoutSessionDeadline": "2026-10-30T12:30:00+00:00",
            "reason": "private support reason",
            "checkoutUrl": "https://evil.example/pay",
        },
        appUrl="https://app.example.test/account",
    )
    for fact in ("30.00", "INR", "banking", "6 Nov 2026, 12:00 UTC", "6 Dec 2026, 12:00 UTC",
                 "https://app.example.test/settings/manage-plan?renewalInvoiceId=invoice-1",
                 "Pay renewal invoice", "Checkout session"):
        assert fact in message.htmlContent, fact
    assert "/billing/invoices/" not in message.htmlContent
    assert "private support reason" not in message.htmlContent
    assert "evil.example" not in message.htmlContent


def test_cancellation_uses_final_paid_end():
    message = render(
        "monthly_cancellation_confirmation",
        {"finalPaidEnd": "2026-12-06T12:00:00+00:00"},
    )
    assert "6 Dec 2026, 12:00 UTC" in message.htmlContent


def test_topup_receipt_never_promises_subscription_access():
    message = render(
        "payment_receipt",
        {"purpose": "topup", "amount": 3000, "currency": "INR", "tokens": 10000},
    )
    assert "10000" in message.htmlContent
    assert "does not extend subscription access" in message.htmlContent


def test_future_refund_preserves_current_access_for_both_confirmations():
    for notificationType in ("subscription_refund_initiated", "subscription_refund_processed"):
        message = render(
            notificationType,
            {"currentAccessPreserved": True, "amount": 2000, "currency": "INR",
             "cutoff": PERIOD_END},
        )
        assert "current paid access continues" in message.htmlContent


def test_revoked_receipt_describes_financial_history():
    message = render(
        "payment_receipt",
        {"purpose": "renewal", "serviceRevoked": True, "amount": 3000, "currency": "INR"},
    )
    assert "payment history" in message.htmlContent
    assert "does not restore" in message.htmlContent


def test_each_email_links_to_the_page_that_handles_it():
    def link(notificationType, metadata=None):
        return render(notificationType, metadata, appUrl="https://app.example.test/").htmlContent

    assert "https://app.example.test/settings/manage-plan?renewalInvoiceId=inv_2" in link(
        "monthly_renewal_reminder", {"invoiceId": "inv_2"})
    assert "https://app.example.test/settings/manage-plan" in link("monthly_subscription_expired")
    for notificationType in ("payment_receipt", "monthly_cancellation_confirmation",
                             "subscription_refund_processed"):
        assert "https://app.example.test/settings/billing-details" in link(
            notificationType, {"invoiceId": "inv_3"})


def test_every_email_has_a_plain_text_part_and_the_support_contact():
    for notificationType in BILLING_TYPES:
        message = render(
            notificationType,
            {"amount": 3000, "currency": "INR", "finalPaidEnd": PERIOD_END,
             "startedAt": PERIOD_END},
        )
        assert message.textContent
        assert not re.search(r"<(table|td|a |p |h1|html)", message.textContent, re.I)
        assert message.subject in message.textContent
        assert "nubrixai@reviveanalytics.com" in message.htmlContent
        assert "Questions? Contact nubrixai@reviveanalytics.com." in message.textContent


def test_refund_emails_render_amount_and_date_without_invoice_ids():
    hidden = {
        "invoiceId": "inv_secret_1", "attemptId": "7d0f8a52-attempt-secret",
        "paymentId": "pay_secret_1", "providerPaymentId": "pay_secret_2",
        "orderId": "order_secret_1", "operationId": "op-secret-1",
    }

    def renderRefund(notificationType, metadata):
        return render(notificationType, {**hidden, **metadata})

    started = renderRefund(
        "payment_refund_started",
        {"amount": 472000, "currency": "INR", "reason": "duplicate", "purpose": "renewal",
         "startedAt": "2026-10-21T09:30:00+00:00"},
    )
    completed = renderRefund(
        "payment_refund_completed",
        {"amount": 5000, "currency": "USD", "reason": "paid_after_close", "purpose": "topup",
         "startedAt": "2026-10-21T09:30:00+00:00", "completedAt": "2026-10-24T11:05:00+00:00"},
    )
    for part in (started.htmlContent, started.textContent):
        assert "A refund of INR 4720.00 is on its way — started 21 Oct 2026, 09:30 UTC." in part
    for part in (completed.htmlContent, completed.textContent):
        for fact in ("USD 50.00", "21 Oct 2026, 09:30 UTC", "24 Oct 2026, 11:05 UTC"):
            assert fact in part, fact
    for message in (started, completed):
        for identity in hidden.values():
            assert identity not in message.htmlContent
            assert identity not in message.textContent
        assert "https://app.example.test/settings/billing-details" in message.htmlContent
        assert "Refund amount" in message.textContent
    assert started.subject != completed.subject


def test_refund_emails_require_the_amount_and_start_date():
    for notificationType in ("payment_refund_started", "payment_refund_completed"):
        for metadata in (
            {"currency": "INR", "startedAt": PERIOD_END},
            {"amount": 3000, "currency": "INR"},
            {"amount": 3000, "currency": "INR", "startedAt": "soon"},
        ):
            assertRejected(
                "INVALID_REFUND_FACTS",
                notificationType=notificationType, name="A", metadata=metadata, appUrl=APP,
            )


def test_invalid_amount_service_date_and_invoice_id_are_rejected():
    for metadata in (
        {"amount": -1, "currency": "INR"},
        {"amount": 10.5, "currency": "INR"},
        {"amount": True, "currency": "INR"},
        {"amount": None, "currency": "INR"},
        {"amount": 100, "currency": "inr"},
        {"amount": 100},
    ):
        assertRejected(
            "INVALID_AMOUNT",
            notificationType="payment_receipt", name="A", metadata=metadata, appUrl=APP,
        )
    assertRejected(
        "INVALID_SERVICE_DATE",
        notificationType="payment_receipt", name="A",
        metadata={"finalPaidEnd": "soon"}, appUrl=APP,
    )
    for invoiceId in ("bad id", "a/b", "x" * 129):
        assertRejected(
            "INVALID_INVOICE_ID",
            notificationType="monthly_renewal_ready", name="A",
            metadata={"invoiceId": invoiceId}, appUrl=APP,
        )


def test_trialWarningMatchesDeployedCopy():
    message = renderTrialExpiryWarning(
        name="Asha", trialEndDate="2026-11-09T00:00:00Z",
        appUrl="https://www.nubrixai.com", senderName="Nubrix AI",
        now=datetime.datetime(2026, 11, 7, 10, 0, tzinfo=datetime.timezone.utc),
    )
    assert message.subject == "⏳ Your Free Trial Ends in 2 Days"
    assert message.textContent is None
    assert ('expires in <strong class="highlight">2 days</strong> '
            '(on <strong>November 9, 2026</strong>)') in message.htmlContent
    assert 'href="https://www.nubrixai.com/settings/manage-plan"' in message.htmlContent
    assert "Hi Asha! We hope" in message.htmlContent
    assert "© 2026 Nubrix AI. All rights reserved." in message.htmlContent


def test_trial_warning_counts_whole_utc_calendar_days_with_floor_of_zero():
    def subject(now):
        return renderTrialExpiryWarning(
            name="A", trialEndDate="2026-11-09T23:59:00+00:00",
            appUrl=APP, senderName="S", now=now,
        ).subject

    utc = datetime.timezone.utc
    assert subject(datetime.datetime(2026, 11, 7, 23, 59, tzinfo=utc)) == "⏳ Your Free Trial Ends in 2 Days"
    assert subject(datetime.datetime(2026, 11, 9, 0, 0, tzinfo=utc)) == "⏳ Your Free Trial Ends in 0 Days"
    assert subject(datetime.datetime(2026, 11, 12, 0, 0, tzinfo=utc)) == "⏳ Your Free Trial Ends in 0 Days"


def test_trialWarningEscapesRecipientName():
    message = renderTrialExpiryWarning(
        name='<script>x</script>&"', trialEndDate="2026-11-09T00:00:00Z",
        appUrl=APP, senderName="S",
    )
    assert "&lt;script&gt;x&lt;/script&gt;&amp;&quot;" in message.htmlContent
    assert "<script>x" not in message.htmlContent


def test_trial_warning_rejects_bad_inputs():
    with pytest.raises(ValueError) as error:
        renderTrialExpiryWarning(name="A", trialEndDate="soon", appUrl=APP, senderName="S")
    assert str(error.value) == "INVALID_TRIAL_END"
    with pytest.raises(ValueError) as error:
        renderTrialExpiryWarning(name="  ", trialEndDate="2026-11-09T00:00:00Z",
                                 appUrl=APP, senderName="S")
    assert str(error.value) == "INVALID_RECIPIENT_NAME"
    with pytest.raises(ValueError) as error:
        renderTrialExpiryWarning(name="A", trialEndDate="2026-11-09T00:00:00Z",
                                 appUrl="javascript:alert(1)", senderName="S")
    assert str(error.value) == "INVALID_APP_URL"


def test_actionUrlJoinsBaseWithOrWithoutTrailingSlash():
    for appUrl in ("https://www.nubrixai.com", "https://www.nubrixai.com/"):
        message = render("monthly_renewal_ready", {"invoiceId": "inv_1"}, appUrl=appUrl)
        assert "https://www.nubrixai.com/settings/manage-plan?renewalInvoiceId=inv_1" in message.htmlContent
        assert "https://www.nubrixai.com/settings/manage-plan?renewalInvoiceId=inv_1" in message.textContent
        warning = renderTrialExpiryWarning(
            name="A", trialEndDate="2026-11-09T00:00:00Z", appUrl=appUrl, senderName="S")
        assert 'href="https://www.nubrixai.com/settings/manage-plan"' in warning.htmlContent


def test_formatAmountUsesCurrencyMinorUnits():
    inr = render("payment_receipt", {"amount": 472000, "currency": "INR"})
    jpy = render("payment_receipt", {"amount": 500, "currency": "JPY"})
    assert "Invoice amount: INR 4720.00" in inr.textContent
    assert "Invoice amount: JPY 500\n" in jpy.textContent


def test_formatDateIsExplicitUtc():
    message = render("monthly_cancellation_confirmation", {"finalPaidEnd": "2026-11-06T12:00:00Z"})
    assert "Final paid access ends: 6 Nov 2026, 12:00 UTC" in message.textContent
    offset = render("monthly_cancellation_confirmation", {"finalPaidEnd": "2026-11-06T17:30:00+05:30"})
    assert "Final paid access ends: 6 Nov 2026, 12:00 UTC" in offset.textContent


def test_formatDateMatchesIntlEnGbSeptemberAbbreviation():
    message = render("monthly_cancellation_confirmation", {"finalPaidEnd": "2026-09-05T00:05:00Z"})
    assert "Final paid access ends: 5 Sept 2026, 00:05 UTC" in message.textContent


WELCOME_SUBJECT = "Welcome to Nubrix AI: your free trial has started"
WELCOME_P1 = "Your Nubrix AI free trial is active. It includes our full pack of industry experts, so you can connect your data, build custom charts, schedule reports and run transformation threads across all of them."
WELCOME_P2 = "When the trial ends, your access simply pauses. No payment is taken unless you choose a plan, and your projects and settings stay intact if you upgrade."


def welcome(**overrides):
    kwargs = dict(
        name="Asha",
        trialEndsAt="2026-11-09T10:00:00Z",
        experts=["banking", "supplychain"],
        appUrl="https://www.nubrixai.com",
    )
    kwargs.update(overrides)
    return renderFreeTrialWelcome(**kwargs)


def test_freeTrialWelcomeRendersApprovedCopy():
    message = welcome()
    html = message.htmlContent
    assert message.subject == WELCOME_SUBJECT
    assert f"<title>{WELCOME_SUBJECT}</title>" in html
    assert re.search(r"<h1[^>]*>Your free trial has started</h1>", html)
    assert "Hello Asha," in html
    assert f'<div style="display:none;max-height:0;overflow:hidden;">{WELCOME_P1}</div>' in html
    assert f">{WELCOME_P1}</p>" in html
    assert f">{WELCOME_P2}</p>" in html
    assert re.search(r">Free trial ends</td><td[^>]*>9 Nov 2026, 10:00 UTC</td>", html)
    assert re.search(r">Experts included</td><td[^>]*>Banking, Supply Chain</td>", html)
    assert html.index("Free trial ends") < html.index("Experts")
    assert 'href="https://www.nubrixai.com/dashboard"' in html
    assert ">Open your dashboard</a>" in html
    assert 'Or open <a href="https://www.nubrixai.com/"' in html
    assert ">your Nubrix account</a>." in html
    assert (
        "You are receiving this email because you started a Nubrix AI free trial. Questions? Contact "
        '<a href="mailto:nubrixai@reviveanalytics.com" style="color:#64748b;">nubrixai@reviveanalytics.com</a>.'
    ) in html
    assert message.textContent == "\n".join([
        WELCOME_SUBJECT, "", "Hello Asha,", "", WELCOME_P1, "", WELCOME_P2, "",
        "Free trial ends: 9 Nov 2026, 10:00 UTC", "Experts included: Banking, Supply Chain", "",
        "Open your dashboard: https://www.nubrixai.com/dashboard", "",
        "Questions? Contact nubrixai@reviveanalytics.com.",
    ])
    withSlash = welcome(appUrl="https://www.nubrixai.com/")
    assert 'href="https://www.nubrixai.com/dashboard"' in withSlash.htmlContent
    assert "Open your dashboard: https://www.nubrixai.com/dashboard" in withSlash.textContent


def test_freeTrialWelcomeEscapesRecipientName():
    message = welcome(name="<script>alert(\"x\")</script> & O'Neil")
    assert "<script>" not in message.htmlContent
    assert "Hello &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; O&#39;Neil," in message.htmlContent
    assert "Hello <script>alert(\"x\")</script> & O'Neil," in message.textContent


def test_freeTrialWelcomeOmitsEmptyExperts():
    for experts in ([], ()):
        message = welcome(experts=experts)
        assert "Experts" not in message.htmlContent
        assert "Experts" not in message.textContent
        assert "Free trial ends: 9 Nov 2026, 10:00 UTC" in message.textContent
    unknown = welcome(experts=["banking", "mystery"])
    assert "Banking, mystery" in unknown.textContent
    parsed = welcome(trialEndsAt=datetime.datetime(2026, 11, 9, 10, 0, tzinfo=datetime.timezone.utc))
    assert "Free trial ends: 9 Nov 2026, 10:00 UTC" in parsed.textContent


def test_freeTrialWelcomeRejectsBadInput():
    for kwargs, code in (
        ({"name": "  "}, "INVALID_RECIPIENT_NAME"),
        ({"name": ""}, "INVALID_RECIPIENT_NAME"),
        ({"trialEndsAt": "not-a-date"}, "INVALID_TRIAL_END"),
        ({"trialEndsAt": None}, "INVALID_TRIAL_END"),
        ({"appUrl": "javascript:alert(1)"}, "INVALID_APP_URL"),
        ({"appUrl": ""}, "INVALID_APP_URL"),
    ):
        with pytest.raises(ValueError) as error:
            welcome(**kwargs)
        assert str(error.value) == code
