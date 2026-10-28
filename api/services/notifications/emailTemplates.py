"""Pure renderers for billing and trial-warning emails (no I/O, no env reads).

Ported from the Supabase edge functions ``billingEmail`` and ``warningEmail``.
Validation failures raise ``ValueError(<CODE>)`` with the edge function's codes.
"""

__all__ = [
    "BILLING_TYPES",
    "RenderedEmail",
    "renderBillingMessage",
    "renderTrialExpiryWarning",
]


import calendar
import datetime
import re
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit, urlunsplit


BILLING_TYPES: tuple[str, ...] = (
    "annual_renewal_upcoming",
    "monthly_renewal_ready", "monthly_renewal_reminder", "monthly_subscription_expired",
    "payment_receipt", "monthly_cancellation_confirmation",
    "subscription_refund_initiated", "subscription_refund_processed",
    "payment_refund_started", "payment_refund_completed",
)

SUPPORT_EMAIL = "nubrixai@reviveanalytics.com"

# Why an automatic refund happens, by the capture rules' reason; nothing else from metadata is shown.
REFUND_REASONS = {
    "duplicate": "You paid more than once for the same purchase, so the extra payment is returned in full.",
    "paid_after_close": "This payment was made after its checkout had closed, so it is returned in full.",
    "not_deliverable": "This purchase could no longer be provided, so the payment is returned in full.",
    "account_closed": "Your account is closed, so the payment is returned in full.",
}

_MESSAGES = {
    "annual_renewal_upcoming": ("Your Pro yearly coverage is ending soon", "Your next yearly invoice is available in your account. Renewal is a manual purchase; no automatic payment will be taken. Your current paid access continues until its paid end."),
    "monthly_renewal_ready": ("Your monthly renewal invoice is ready", "Pay before your current period ends to keep access for the next calendar month. Early payment preserves your remaining paid days."),
    "monthly_renewal_reminder": ("Your monthly subscription ends tomorrow", "Your renewal invoice is still unpaid. Complete checkout before expiry to keep access. No automatic payment will be taken."),
    "monthly_subscription_expired": ("Your monthly subscription has expired", "Paid access has ended. You can purchase a new subscription from your account. Your stored purchased credits are preserved."),
    "payment_receipt": ("Your payment was received", "Your payment is confirmed. Your account shows the paid service dates and invoice. An early renewal starts at your existing expiry."),
    "monthly_cancellation_confirmation": ("Monthly renewal cancelled", "We have stopped renewal bills and reminders. Your already-paid access continues until its final paid end. Cancellation does not automatically issue a refund."),
    "subscription_refund_initiated": ("Your approved refund has been initiated", "Support approved the unused-time refund. The affected service access has ended; processing at the payment provider is tracked separately."),
    "subscription_refund_processed": ("Your approved refund has been processed", "The payment provider has processed your approved unused-time refund. Bank posting times may vary."),
    "payment_refund_started": ("Your refund is on its way", ""),
    "payment_refund_completed": ("Your refund has been processed", ""),
}

# Minor-unit digits by currency; production only sees INR and USD.
_ZERO_DECIMAL_CURRENCIES = {"JPY": 0, "KRW": 0}

# Intl.DateTimeFormat("en-GB", {month: "short"}) abbreviates September as "Sept".
_SHORT_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sept", "Oct", "Nov", "Dec")

_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
_INVOICE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    htmlContent: str
    textContent: str | None


def _escape(value) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"[&<>\"']", lambda match: _ESCAPES[match.group()], text)


def _parseDate(value, errorCode: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        raise ValueError(errorCode) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _formatAmount(metadata: dict) -> str:
    """e.g. "INR 4720.00": the currency's own minor-unit digits."""
    amount = metadata.get("amount")
    currency = str(metadata.get("currency") or "")
    if (
        not isinstance(amount, int) or isinstance(amount, bool) or amount < 0
        or not re.fullmatch(r"[A-Z]{3}", currency)
    ):
        raise ValueError("INVALID_AMOUNT")
    digits = _ZERO_DECIMAL_CURRENCIES.get(currency, 2)
    return f"{currency} {amount / 10 ** digits:.{digits}f}"


def _formatDate(date: datetime.datetime) -> str:
    """e.g. "6 Nov 2026, 12:00 UTC": explicit zone, since recipients live in different time zones."""
    date = date.astimezone(datetime.timezone.utc)
    return f"{date.day} {_SHORT_MONTHS[date.month - 1]} {date.year}, {date:%H:%M} UTC"


def _parseAppUrl(appUrl):
    try:
        parts = urlsplit(str(appUrl))
        hostname = parts.hostname
    except ValueError:
        raise ValueError("INVALID_APP_URL") from None
    if parts.scheme not in ("http", "https") or not hostname or parts.username or parts.password:
        raise ValueError("INVALID_APP_URL")
    return urlunsplit(parts._replace(path=parts.path or "/"))


def renderBillingMessage(*, notificationType: str, name: str, metadata: dict, appUrl: str) -> RenderedEmail:
    if notificationType not in BILLING_TYPES:
        raise ValueError("UNSUPPORTED_NOTIFICATION_TYPE")
    name = str(name or "").strip()
    if not name:
        raise ValueError("INVALID_RECIPIENT_NAME")
    metadata = metadata if isinstance(metadata, dict) else {}

    subject, defaultText = _MESSAGES[notificationType]
    if metadata.get("billingInterval") == "year":
        if notificationType == "monthly_renewal_ready":
            subject = "Your Pro yearly renewal invoice is ready"
            defaultText = "Pay before your current coverage ends to keep access for the next year. Early payment preserves your remaining paid days."
        elif notificationType == "monthly_renewal_reminder":
            subject = "Your Pro yearly coverage ends tomorrow"
        elif notificationType == "monthly_subscription_expired":
            subject = "Your Pro yearly coverage has expired"
        elif notificationType == "monthly_cancellation_confirmation":
            subject = "Pro renewal reminders stopped"

    topup = notificationType == "payment_receipt" and metadata.get("purpose") == "topup"
    refund = notificationType.startswith("subscription_refund_")
    # Automatic full refunds of a payment that was never granted: amount and dates only, never an identity.
    paymentRefund = notificationType.startswith("payment_refund_")
    text = defaultText
    if paymentRefund:
        if "amount" not in metadata or not metadata.get("startedAt"):
            raise ValueError("INVALID_REFUND_FACTS")
        started = _parseDate(metadata["startedAt"], "INVALID_REFUND_FACTS")
        amount = _formatAmount(metadata)
        why = REFUND_REASONS.get(str(metadata.get("reason") or ""), "")
        text = (
            f"A refund of {amount} is on its way — started {_formatDate(started)}."
            if notificationType == "payment_refund_started"
            else f"The payment provider has processed your refund of {amount}, started {_formatDate(started)}."
        ) + (f" {why}" if why else "") + " Bank posting times may vary."
    elif topup:
        text = "Your purchased top-up credits have been stored. This payment does not extend subscription access or refill your subscription allowance."
    elif notificationType == "payment_receipt" and metadata.get("serviceRevoked") is True:
        text = "This receipt records your payment history. The affected service has since ended or been refunded; this receipt does not restore subscription access."
    elif refund and metadata.get("currentAccessPreserved") is True:
        text = (
            "Support approved a refund for your unused future period. That future period has been removed; your current paid access continues. "
            + ("The provider has processed the refund; bank posting times may vary."
               if notificationType == "subscription_refund_processed"
               else "Payment provider processing is tracked separately.")
        )
    elif refund:
        text += " Your affected subscription access ended at the cutoff shown below."

    baseUrl = _parseAppUrl(appUrl)
    facts: list[tuple[str, str]] = []

    def fact(label, value):
        facts.append((label, "" if value is None else str(value)))

    def dateFact(label, value):
        if value is None or value == "":
            return
        fact(label, _formatDate(_parseDate(value, "INVALID_SERVICE_DATE")))

    if "amount" in metadata:
        fact("Refund amount" if refund or paymentRefund else "Invoice amount", _formatAmount(metadata))
    if isinstance(metadata.get("domains"), list) and not topup:
        fact("Experts", ", ".join(str(domain) for domain in metadata["domains"]))
    if topup:
        fact("Purchased credits", metadata.get("tokens"))
    if not topup:
        dateFact("Purchased service starts", metadata.get("periodStart"))
        dateFact("Purchased service ends", metadata.get("serviceEnd"))
        dateFact("Current service starts", metadata.get("currentStart"))
        dateFact("Current service ends", metadata.get("currentEnd"))
        dateFact("Next paid period starts", metadata.get("nextStart"))
        dateFact("Next paid period ends", metadata.get("nextEnd"))
    dateFact("Final paid access ends", metadata.get("finalPaidEnd"))
    dateFact("Renewal payment deadline", metadata.get("renewalDeadline"))
    dateFact("Checkout session deadline", metadata.get("checkoutSessionDeadline"))
    if refund:
        dateFact("Service cutoff", metadata.get("cutoff"))
    if paymentRefund:
        dateFact("Refund started", metadata.get("startedAt"))
        dateFact("Refund completed", metadata.get("completedAt"))

    # Each email opens the app page that handles it.
    invoiceId = ""
    if metadata.get("invoiceId") and not paymentRefund:
        invoiceId = str(metadata["invoiceId"])
        if not _INVOICE_ID.fullmatch(invoiceId):
            raise ValueError("INVALID_INVOICE_ID")
    renewal = notificationType in (
        "annual_renewal_upcoming", "monthly_renewal_ready", "monthly_renewal_reminder")
    if renewal:
        actionLabel = "Pay renewal invoice"
        actionPath = (f"/settings/manage-plan?renewalInvoiceId={quote(invoiceId, safe='')}"
                      if invoiceId else "/settings/manage-plan")
    elif notificationType == "monthly_subscription_expired":
        actionLabel, actionPath = "Choose a plan", "/settings/manage-plan"
    else:
        actionLabel, actionPath = "View billing history", "/settings/billing-details"
    actionUrl = urljoin(baseUrl, actionPath)

    rows = "".join(
        f'<tr><td style="padding:8px 0;color:#64748b;font-size:14px;">{_escape(label)}</td>'
        f'<td style="padding:8px 0;color:#0f172a;font-size:14px;font-weight:600;text-align:right;">{_escape(value)}</td></tr>'
        for label, value in facts
    )
    factsTable = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #e2e8f0;border-bottom:1px solid #e2e8f0;margin:0 0 24px;">{rows}</table>'
        if rows else ""
    )
    htmlContent = (
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_escape(subject)}</title></head>'
        "<body style=\"margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;\">"
        f'<div style="display:none;max-height:0;overflow:hidden;">{_escape(text)}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 16px;"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:12px;overflow:hidden;">'
        '<tr><td style="background:#2563eb;padding:20px 32px;color:#ffffff;font-size:18px;font-weight:700;">Nubrix AI</td></tr>'
        '<tr><td style="padding:32px;">'
        f'<h1 style="margin:0 0 16px;color:#0f172a;font-size:22px;line-height:1.3;">{_escape(subject)}</h1>'
        f'<p style="margin:0 0 12px;color:#334155;font-size:15px;line-height:1.6;">Hello {_escape(name)},</p>'
        f'<p style="margin:0 0 24px;color:#334155;font-size:15px;line-height:1.6;">{_escape(text)}</p>'
        f'{factsTable}'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="border-radius:8px;background:#2563eb;">'
        f'<a href="{_escape(actionUrl)}" style="display:inline-block;padding:12px 24px;color:#ffffff;font-size:15px;font-weight:600;text-decoration:none;">{_escape(actionLabel)}</a>'
        '</td></tr></table>'
        f'<p style="margin:24px 0 0;color:#64748b;font-size:13px;line-height:1.6;">Or open <a href="{_escape(baseUrl)}" style="color:#2563eb;">your Nubrix account</a>.</p>'
        '</td></tr>'
        '<tr><td style="padding:20px 32px;background:#f8fafc;color:#94a3b8;font-size:12px;line-height:1.6;">'
        'You are receiving this email about your Nubrix AI subscription. Questions? Contact '
        f'<a href="mailto:{SUPPORT_EMAIL}" style="color:#64748b;">{SUPPORT_EMAIL}</a>.</td></tr>'
        '</table></td></tr></table></body></html>'
    )
    textContent = "\n".join([
        subject, "", f"Hello {name},", "", text, "",
        *[f"{label}: {value}" for label, value in facts], "", f"{actionLabel}: {actionUrl}", "",
        f"Questions? Contact {SUPPORT_EMAIL}.",
    ])
    return RenderedEmail(subject=subject, htmlContent=htmlContent, textContent=textContent)


_TRIAL_WARNING_STYLE = """
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      background-color: #f8fafc;
      margin: 0;
      padding: 40px 20px;
    }
    .email-container {
      background: #ffffff;
      padding: 40px;
      border-radius: 16px;
      max-width: 580px;
      margin: 0 auto;
      box-shadow: 0px 4px 12px rgba(0,0,0,0.03);
      border: 1px solid #f1f5f9;
      text-align: left;
    }
    .logo-container {
      text-align: center;
      margin-bottom: 30px;
    }
    .logo {
      font-size: 24px;
      font-weight: 800;
      color: #0f172a;
      letter-spacing: -0.5px;
    }
    .logo span {
      color: #2563eb;
    }
    h2 {
      color: #0f172a;
      font-size: 24px;
      font-weight: 700;
      margin-top: 0;
      margin-bottom: 16px;
      line-height: 1.3;
    }
    p {
      color: #475569;
      font-size: 16px;
      line-height: 1.6;
      margin-top: 0;
      margin-bottom: 24px;
    }
    .highlight {
      color: #ef4444;
      font-weight: 600;
    }
    .warning-details {
      background-color: #fff1f2;
      border-left: 4px solid #ef4444;
      padding: 16px 20px;
      border-radius: 0 8px 8px 0;
      margin-bottom: 24px;
    }
    .warning-details p {
      margin: 0;
      color: #991b1b;
      font-weight: 500;
    }
    .cta-container {
      text-align: center;
      margin: 32px 0;
    }
    .button {
      background-color: #2563eb;
      color: #ffffff !important;
      padding: 14px 32px;
      border-radius: 8px;
      text-decoration: none;
      font-weight: 600;
      font-size: 16px;
      display: inline-block;
      box-shadow: 0px 4px 10px rgba(37,99,235,0.2);
    }
    .footer {
      margin-top: 40px;
      padding-top: 24px;
      border-top: 1px solid #f1f5f9;
      font-size: 13px;
      color: #94a3b8;
      text-align: center;
      line-height: 1.5;
    }
"""


def _utcCalendarDaysUntil(value: datetime.datetime, now: datetime.datetime) -> int:
    return max(0, (value.astimezone(datetime.timezone.utc).date()
                   - now.astimezone(datetime.timezone.utc).date()).days)


def renderTrialExpiryWarning(
    *,
    name: str,
    trialEndDate: str,
    appUrl: str,
    senderName: str,
    now: datetime.datetime | None = None,
) -> RenderedEmail:
    name = str(name or "").strip()
    if not name:
        raise ValueError("INVALID_RECIPIENT_NAME")
    trialEnd = _parseDate(trialEndDate, "INVALID_TRIAL_END")
    billingUrl = urljoin(_parseAppUrl(appUrl), "/billing")
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)

    daysLeft = _utcCalendarDaysUntil(trialEnd, now)
    trialEndFormatted = f"{calendar.month_name[trialEnd.month]} {trialEnd.day}, {trialEnd.year}"

    htmlContent = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Your Free Trial Is Ending Soon</title>
  <style>{_TRIAL_WARNING_STYLE}  </style>
</head>
<body>
  <div class="email-container">
    <div class="logo-container">
      <div class="logo">NubrixAI<span>.</span></div>
    </div>

    <h2>⏳ Your Free Trial Is Ending Soon</h2>

    <p>
      Hi {_escape(name)}! We hope you've been getting incredible value out of Nubrix AI over the past several days.
    </p>

    <div class="warning-details">
      <p>
        Just a quick heads up: your free trial expires in <strong class="highlight">{daysLeft} days</strong> (on <strong>{trialEndFormatted}</strong>).
      </p>
    </div>

    <p>
      To prevent any interruption to your scheduled reports, custom charts, and active transformation threads, upgrade to one of our premium plans today. Your existing data and configurations will remain completely intact.
    </p>

    <div class="cta-container">
      <a href="{_escape(billingUrl)}" class="button">Upgrade Your Plan</a>
    </div>

    <p>
      No action is required if you choose not to subscribe. Your access will simply pause on the trial end date, and you won't be charged anything.
    </p>

    <div class="footer">
      <p>© 2026 {_escape(senderName)}. All rights reserved.</p>
    </div>
  </div>
</body>
</html>
"""
    return RenderedEmail(
        subject=f"⏳ Your Free Trial Ends in {daysLeft} Days",
        htmlContent=htmlContent,
        textContent=None,
    )
