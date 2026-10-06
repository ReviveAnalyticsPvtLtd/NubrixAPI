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
