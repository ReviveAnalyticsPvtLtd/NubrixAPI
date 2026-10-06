import { BILLING_TYPES, parseBillingRequest, renderBillingMessage } from "./contract.ts";

const base = {
  deliveryId:"5f5618db-6b67-4117-b392-e6cbf38e3758", templateVersion:"1",
  email:"person@example.test", name:"<script>alert('x')</script>",
  periodEnd:"2026-11-06T12:00:00+00:00",
  trackingTag:"nubrix_delivery:5f5618db-6b67-4117-b392-e6cbf38e3758",
};

Deno.test("all billing types render their own escaped message", () => {
  const subjects = new Set<string>();
  for (const notificationType of BILLING_TYPES) {
    const request = parseBillingRequest({...base,notificationType});
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
  for (const fact of ["30.00", "INR", "banking", "2026-11-06", "2026-12-06", "/billing/invoices/invoice-1", "Checkout session"])
    if (!message.htmlContent.includes(fact)) throw new Error(`missing fact ${fact}`);
  if (message.htmlContent.includes("private support reason") || message.htmlContent.includes("evil.example")) throw new Error("untrusted data rendered");
});

Deno.test("cancellation uses final paid end", () => {
  const message = renderBillingMessage(parseBillingRequest({...base,notificationType:"monthly_cancellation_confirmation",
    metadata:{finalPaidEnd:"2026-12-06T12:00:00+00:00"}}),"https://app.example.test");
  if (!message.htmlContent.includes("2026-12-06")) throw new Error("final paid time omitted");
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
