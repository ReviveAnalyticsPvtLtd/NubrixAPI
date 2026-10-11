import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { parseBillingRequest, renderBillingMessage } from "./contract.ts";
import { acceptedResponse } from "../warningEmail/contract.ts";
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {status,headers:{"Content-Type":"application/json"}});
serve(async req => {
  if (req.method !== "POST") return response({errorCode:"METHOD_NOT_ALLOWED"},405);
  try {
    const request = parseBillingRequest(await req.json());
    const apiKey = Deno.env.get("BREVO_API_KEY");
    const senderEmail = Deno.env.get("BREVO_SENDER_EMAIL");
    const senderName = Deno.env.get("BREVO_SENDER_NAME");
    const appUrl = Deno.env.get("APP_URL");
    if (!apiKey || !senderEmail || !senderName || !appUrl) return response({errorCode:"FUNCTION_NOT_CONFIGURED"},503);
    let provider: Response;
    try {
      provider = await fetch("https://api.brevo.com/v3/smtp/email", {
        method:"POST",headers:{"Content-Type":"application/json","api-key":apiKey},
        body:JSON.stringify({sender:{email:senderEmail,name:senderName},to:[{email:request.email,name:request.name}],
          ...renderBillingMessage(request,appUrl),tags:[request.trackingTag],
          headers:{"X-Mailin-custom":`delivery_id:${request.deliveryId}`}}),signal:AbortSignal.timeout(8000)});
    } catch {
      // No safe retry proof: the durable dispatcher must reconcile by tag.
      return response({errorCode:"AMBIGUOUS_SEND"},504);
    }
    if (!provider.ok) return response({errorCode:`BREVO_HTTP_${provider.status}`},provider.status === 429 ? 429 : provider.status >= 500 ? 503 : 422);
    try {
      const payload = await provider.json();
      return response(acceptedResponse(String(payload.messageId ?? "")));
    } catch {
      return response({errorCode:"AMBIGUOUS_SEND"},504);
    }
  } catch (error) {
    const code = error instanceof Error ? error.message : "INTERNAL_ERROR";
    const invalid = /^(INVALID_|UNSUPPORTED_)/.test(code);
    return response({errorCode:invalid ? code : "INTERNAL_ERROR"},invalid ? 400 : 500);
  }
});
