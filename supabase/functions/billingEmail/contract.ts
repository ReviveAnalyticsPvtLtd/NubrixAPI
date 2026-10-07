export const BILLING_TYPES = [
  "monthly_renewal_ready", "monthly_renewal_reminder", "monthly_subscription_expired",
  "payment_receipt", "monthly_cancellation_confirmation",
  "subscription_refund_initiated", "subscription_refund_processed",
] as const;
type BillingType = typeof BILLING_TYPES[number];
export type BillingRequest = {
  deliveryId: string; notificationType: BillingType; templateVersion: "1";
  email: string; name: string; periodEnd: string; trackingTag: string;
  metadata: Record<string, unknown>;
};
export function parseBillingRequest(value: unknown): BillingRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("INVALID_REQUEST_BODY");
  const body = value as Record<string, unknown>;
  if (!BILLING_TYPES.includes(body.notificationType as BillingType)) throw new Error("UNSUPPORTED_NOTIFICATION_TYPE");
  if (body.templateVersion !== "1") throw new Error("UNSUPPORTED_TEMPLATE_VERSION");
  const deliveryId = String(body.deliveryId ?? "");
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(deliveryId)) throw new Error("INVALID_DELIVERY_ID");
  const email = String(body.email ?? "").trim();
  const name = String(body.name ?? "").trim();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new Error("INVALID_RECIPIENT");
  if (!name) throw new Error("INVALID_RECIPIENT_NAME");
  const periodEnd = String(body.periodEnd ?? "");
  if (Number.isNaN(new Date(periodEnd).getTime())) throw new Error("INVALID_PERIOD_END");
  if (body.trackingTag !== `nubrix_delivery:${deliveryId}`) throw new Error("INVALID_TRACKING_TAG");
  return { deliveryId, notificationType: body.notificationType as BillingType, templateVersion: "1",
    email, name, periodEnd, trackingTag: String(body.trackingTag),
    metadata: body.metadata && typeof body.metadata === "object" && !Array.isArray(body.metadata) ? body.metadata as Record<string,unknown> : {} };
}
function escape(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]!));
}
export function renderBillingMessage(request: BillingRequest, appUrl: string) {
  const messages: Record<BillingType, [string, string]> = {
    monthly_renewal_ready: ["Your monthly renewal invoice is ready", "Pay before your current period ends to keep access for the next calendar month. Early payment preserves your remaining paid days."],
    monthly_renewal_reminder: ["Your monthly subscription ends tomorrow", "Your renewal invoice is still unpaid. Complete checkout before expiry to keep access. No automatic payment will be taken."],
    monthly_subscription_expired: ["Your monthly subscription has expired", "Paid access has ended. You can purchase a new subscription from your account. Your stored purchased credits are preserved."],
    payment_receipt: ["Your payment was received", "Your payment is confirmed. Your account shows the paid service dates and invoice. An early renewal starts at your existing expiry."],
    monthly_cancellation_confirmation: ["Monthly renewal cancelled", "We have stopped renewal bills and reminders. Your already-paid access continues until its final paid end. Cancellation does not automatically issue a refund."],
    subscription_refund_initiated: ["Your approved refund has been initiated", "Support approved the unused-time refund. The affected service access has ended; processing at the payment provider is tracked separately."],
    subscription_refund_processed: ["Your approved refund has been processed", "The payment provider has processed your approved unused-time refund. Bank posting times may vary."],
  };
  const [subject, defaultText] = messages[request.notificationType];
  const metadata = request.metadata;
  const topup = request.notificationType === "payment_receipt" && metadata.purpose === "topup";
  const refund = request.notificationType.startsWith("subscription_refund_");
  let text = defaultText;
  if (topup) text = "Your purchased top-up credits have been stored. This payment does not extend subscription access or refill your subscription allowance.";
  else if (request.notificationType === "payment_receipt" && metadata.serviceRevoked === true)
    text = "This receipt records your payment history. The affected service has since ended or been refunded; this receipt does not restore subscription access.";
  else if (refund && metadata.currentAccessPreserved === true)
    text = "Support approved a refund for your unused future period. That future period has been removed; your current paid access continues. " +
      (request.notificationType === "subscription_refund_processed" ? "The provider has processed the refund; bank posting times may vary." : "Payment provider processing is tracked separately.");
  else if (refund) text += " Your affected subscription access ended at the cutoff shown below.";
  const url = new URL(appUrl);
  if ((url.protocol !== "https:" && url.protocol !== "http:") || url.username || url.password) throw new Error("FUNCTION_NOT_CONFIGURED");
  const facts: string[] = [];
  const fact = (label: string, value: unknown) => facts.push(`<p>${escape(label)}: ${escape(value)}</p>`);
  const dateFact = (label: string, value: unknown) => {
    if (value === undefined || value === null || value === "") return;
    const date = new Date(String(value));
    if (Number.isNaN(date.getTime())) throw new Error("INVALID_SERVICE_DATE");
    fact(`${label} (UTC)`, date.toISOString());
  };
  if (metadata.amount !== undefined) {
    const currency = String(metadata.currency ?? "");
    if (!Number.isSafeInteger(metadata.amount) || Number(metadata.amount) < 0 || !/^[A-Z]{3}$/.test(currency)) throw new Error("INVALID_AMOUNT");
    const digits = new Intl.NumberFormat("en", {style:"currency", currency}).resolvedOptions().maximumFractionDigits ?? 2;
    fact(refund ? "Refund amount" : "Invoice amount", `${currency} ${(Number(metadata.amount) / 10 ** digits).toFixed(digits)}`);
  }
  if (Array.isArray(metadata.domains) && !topup) fact("Experts", metadata.domains.join(", "));
  if (topup) fact("Purchased credits", metadata.tokens);
  if (!topup) {
    dateFact("Purchased service starts", metadata.periodStart);
    dateFact("Purchased service ends", metadata.serviceEnd);
    dateFact("Current service starts", metadata.currentStart);
    dateFact("Current service ends", metadata.currentEnd);
    dateFact("Next paid period starts", metadata.nextStart);
    dateFact("Next paid period ends", metadata.nextEnd);
  }
  dateFact("Final paid access ends", metadata.finalPaidEnd);
  dateFact("Renewal payment deadline", metadata.renewalDeadline);
  dateFact("Checkout session deadline", metadata.checkoutSessionDeadline);
  if (refund) dateFact("Service cutoff", metadata.cutoff);
  let invoiceLink = "";
  if (metadata.invoiceId) {
    const invoiceId = String(metadata.invoiceId);
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(invoiceId)) throw new Error("INVALID_INVOICE_ID");
    const invoiceUrl = new URL(`/billing/invoices/${encodeURIComponent(invoiceId)}`, url);
    invoiceLink = `<p><a href="${escape(invoiceUrl.href)}">View your invoice</a></p>`;
  }
  return { subject, htmlContent: `<html><body><h1>${escape(subject)}</h1><p>Hello ${escape(request.name)},</p><p>${escape(text)}</p>${facts.join("")}${invoiceLink}<p><a href="${escape(url.href)}">Open your account</a></p></body></html>` };
}
