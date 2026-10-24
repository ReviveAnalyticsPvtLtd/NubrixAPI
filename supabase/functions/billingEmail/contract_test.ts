import { BILLING_TYPES, parseBillingRequest, renderBillingMessage } from "./contract.ts";

const base = {
  deliveryId:"5f5618db-6b67-4117-b392-e6cbf38e3758", templateVersion:"1",
  email:"person@example.test", name:"<script>alert('x')</script>",
  periodEnd:"2026-11-06T12:00:00+00:00",
  trackingTag:"nubrix_delivery:5f5618db-6b67-4117-b392-e6cbf38e3758",
};

Deno.test('annual milestones describe Pro with yearly coverage', () => {
  const upcoming = renderBillingMessage(parseBillingRequest({...base,notificationType:'annual_renewal_upcoming',
    metadata:{billingInterval:'year',invoiceId:'annual-invoice'}}),'https://app.example.test');
  if (!upcoming.subject.includes('Pro') || !upcoming.htmlContent.includes('annual-invoice')) throw new Error('missing annual awareness');
  const ready = renderBillingMessage(parseBillingRequest({...base,notificationType:'monthly_renewal_ready',
    metadata:{billingInterval:'year'}}),'https://app.example.test');
  if (ready.subject.includes('monthly') || ready.textContent.includes('next calendar month')) throw new Error('year sold as month');
});

Deno.test("all billing types render their own escaped message", () => {
  const subjects = new Set<string>();
  for (const notificationType of BILLING_TYPES) {
    const request = parseBillingRequest({...base,notificationType,metadata:{amount:3000,currency:"INR",startedAt:base.periodEnd}});
    const message = renderBillingMessage(request,"https://app.example.test/account");
    if (message.htmlContent.includes("<script>") || !message.htmlContent.includes("&lt;script&gt;")) throw new Error("unescaped recipient");
    subjects.add(message.subject);
  }
  if (subjects.size !== BILLING_TYPES.length) throw new Error("template routing mismatch");
});

Deno.test("invalid recipient or tracking identity cannot be sent", () => {
  for (const changes of [{email:"invalid"},{trackingTag:"other"},{deliveryId:"invalid"},
    {periodEnd:"invalid"},{templateVersion:"2"},{notificationType:"trial_expiry_warning"}]) {
    let rejected = false;
    try { parseBillingRequest({...base,notificationType:"payment_receipt",...changes}); }
    catch { rejected = true; }
    if (!rejected) throw new Error("invalid request accepted");
  }
});

Deno.test("unsafe checkout URL cannot be rendered", () => {
  let rejected = false;
  try { renderBillingMessage(parseBillingRequest({...base,notificationType:"payment_receipt"}),"javascript:alert(1)"); }
  catch { rejected = true; }
  if (!rejected) throw new Error("unsafe URL accepted");
});

Deno.test("renewal invoice contains frozen amount experts and service dates", () => {
  const message = renderBillingMessage(parseBillingRequest({...base, notificationType:"monthly_renewal_ready",
    metadata:{amount:3000,currency:"INR",domains:["banking"],invoiceId:"invoice-1",
      nextStart:"2026-11-06T12:00:00+00:00",nextEnd:"2026-12-06T12:00:00+00:00",
      renewalDeadline:base.periodEnd, checkoutSessionDeadline:"2026-10-30T12:30:00+00:00",
      reason:"private support reason",checkoutUrl:"https://evil.example/pay"}}),"https://app.example.test/account");
  for (const fact of ["30.00", "INR", "banking", "6 Nov 2026, 12:00 UTC", "6 Dec 2026, 12:00 UTC",
      "https://app.example.test/settings/manage-plan?renewalInvoiceId=invoice-1", "Pay renewal invoice", "Checkout session"])
    if (!message.htmlContent.includes(fact)) throw new Error(`missing fact ${fact}`);
  if (message.htmlContent.includes("/billing/invoices/")) throw new Error("links to a page the app does not have");
  if (message.htmlContent.includes("private support reason") || message.htmlContent.includes("evil.example")) throw new Error("untrusted data rendered");
});

Deno.test("cancellation uses final paid end", () => {
  const message = renderBillingMessage(parseBillingRequest({...base,notificationType:"monthly_cancellation_confirmation",
    metadata:{finalPaidEnd:"2026-12-06T12:00:00+00:00"}}),"https://app.example.test");
  if (!message.htmlContent.includes("6 Dec 2026, 12:00 UTC")) throw new Error("final paid time omitted");
});

Deno.test("topup receipt never promises subscription access", () => {
  const message = renderBillingMessage(parseBillingRequest({...base,notificationType:"payment_receipt",
    metadata:{purpose:"topup",amount:3000,currency:"INR",tokens:10000}}),"https://app.example.test");
  if (!message.htmlContent.includes("10000") || !message.htmlContent.includes("does not extend subscription access")) throw new Error("topup access wording wrong");
});

Deno.test("future refund preserves current access for both confirmations", () => {
  for (const notificationType of ["subscription_refund_initiated","subscription_refund_processed"]) {
    const message = renderBillingMessage(parseBillingRequest({...base,notificationType,
      metadata:{currentAccessPreserved:true,amount:2000,currency:"INR",cutoff:base.periodEnd}}),"https://app.example.test");
    if (!message.htmlContent.includes("current paid access continues")) throw new Error("current access incorrectly ended");
  }
});

Deno.test("revoked receipt describes financial history", () => {
  const message = renderBillingMessage(parseBillingRequest({...base,notificationType:"payment_receipt",
    metadata:{purpose:"renewal",serviceRevoked:true,amount:3000,currency:"INR"}}),"https://app.example.test");
  if (!message.htmlContent.includes("payment history") || !message.htmlContent.includes("does not restore")) throw new Error("revoked receipt implies active access");
});

Deno.test("each email links to the page that handles it", () => {
  const link = (notificationType: string, metadata: Record<string, unknown> = {}) =>
    renderBillingMessage(parseBillingRequest({...base,notificationType,metadata}),"https://app.example.test/").htmlContent;
  if (!link("monthly_renewal_reminder",{invoiceId:"inv_2"}).includes("https://app.example.test/settings/manage-plan?renewalInvoiceId=inv_2"))
    throw new Error("reminder does not open the renewal invoice");
  if (!link("monthly_subscription_expired").includes("https://app.example.test/settings/manage-plan"))
    throw new Error("expiry does not open plan selection");
  for (const notificationType of ["payment_receipt","monthly_cancellation_confirmation","subscription_refund_processed"])
    if (!link(notificationType,{invoiceId:"inv_3"}).includes("https://app.example.test/settings/billing-details"))
      throw new Error(`${notificationType} does not open billing history`);
});

Deno.test("every email has a plain-text part and the support contact", () => {
  for (const notificationType of BILLING_TYPES) {
    const message = renderBillingMessage(parseBillingRequest({...base,notificationType,
      metadata:{amount:3000,currency:"INR",finalPaidEnd:base.periodEnd,startedAt:base.periodEnd}}),"https://app.example.test");
    if (!message.textContent || /<(table|td|a |p |h1|html)/i.test(message.textContent)) throw new Error("missing or HTML text part");
    if (!message.textContent.includes(message.subject)) throw new Error("text part lacks subject");
    if (!message.htmlContent.includes("nubrixai@reviveanalytics.com")) throw new Error("support contact missing");
  }
});

Deno.test("refund emails render amount and date without invoice ids", () => {
  const hidden = {invoiceId:"inv_secret_1", attemptId:"7d0f8a52-attempt-secret", paymentId:"pay_secret_1",
    providerPaymentId:"pay_secret_2", orderId:"order_secret_1", operationId:"op-secret-1"};
  const render = (notificationType: string, metadata: Record<string, unknown>) =>
    renderBillingMessage(parseBillingRequest({...base,notificationType,metadata:{...hidden,...metadata}}),"https://app.example.test");
  const started = render("payment_refund_started",{amount:472000,currency:"INR",reason:"duplicate",purpose:"renewal",
    startedAt:"2026-10-21T09:30:00+00:00"});
  const completed = render("payment_refund_completed",{amount:5000,currency:"USD",reason:"paid_after_close",purpose:"topup",
    startedAt:"2026-10-21T09:30:00+00:00",completedAt:"2026-10-24T11:05:00+00:00"});
  for (const part of [started.htmlContent, started.textContent])
    if (!part.includes("A refund of INR 4720.00 is on its way — started 21 Oct 2026, 09:30 UTC.")) throw new Error("started copy missing");
  for (const part of [completed.htmlContent, completed.textContent])
    for (const fact of ["USD 50.00", "21 Oct 2026, 09:30 UTC", "24 Oct 2026, 11:05 UTC"])
      if (!part.includes(fact)) throw new Error(`missing ${fact}`);
  for (const message of [started, completed]) {
    for (const identity of Object.values(hidden))
      if (message.htmlContent.includes(identity) || message.textContent.includes(identity)) throw new Error(`identity ${identity} rendered`);
    if (!message.htmlContent.includes("https://app.example.test/settings/billing-details")) throw new Error("refund does not open billing details");
    if (!message.textContent.includes("Refund amount")) throw new Error("refund amount label missing");
  }
  if (started.subject === completed.subject) throw new Error("milestones share a subject");
});

Deno.test("refund emails require the amount and start date", () => {
  for (const notificationType of ["payment_refund_started","payment_refund_completed"])
    for (const metadata of [{currency:"INR",startedAt:base.periodEnd},{amount:3000,currency:"INR"},{amount:3000,currency:"INR",startedAt:"soon"}]) {
      let rejected = false;
      try { renderBillingMessage(parseBillingRequest({...base,notificationType,metadata}),"https://app.example.test"); }
      catch { rejected = true; }
      if (!rejected) throw new Error(`${notificationType} rendered without its facts`);
    }
});
