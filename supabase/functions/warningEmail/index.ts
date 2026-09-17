import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import {
  acceptedResponse,
  assertRequiredConfiguration,
  parseWarningRequest,
  validationResponse,
} from "./contract.ts";


const BREVO_API_KEY = Deno.env.get("BREVO_API_KEY");
const SENDER_EMAIL = Deno.env.get("BREVO_SENDER_EMAIL");
const SENDER_NAME = Deno.env.get("BREVO_SENDER_NAME");
const APP_URL = Deno.env.get("APP_URL");


function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}


function errorResponse(errorCode: string, status: number): Response {
  return jsonResponse({ errorCode }, status);
}


function utcCalendarDaysUntil(value: Date): number {
  const now = new Date();
  const todayUtc = Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate(),
  );
  const valueUtc = Date.UTC(
    value.getUTCFullYear(),
    value.getUTCMonth(),
    value.getUTCDate(),
  );
  return Math.max(0, Math.round((valueUtc - todayUtc) / 86_400_000));
}


serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }


  try {
    const warningRequest = parseWarningRequest(await req.json());
    assertRequiredConfiguration({
      brevoApiKey: BREVO_API_KEY,
      senderEmail: SENDER_EMAIL,
      senderName: SENDER_NAME,
      appUrl: APP_URL,
    });
    if (warningRequest.mode === "validate") {
      return jsonResponse(validationResponse());
    }

    const { email, name, trialStartDate } = warningRequest;
    console.log(
      warningRequest.mode === "send"
        ? `[warningEmail] Accepted durable request deliveryId=${warningRequest.deliveryId}`
        : "[warningEmail] Accepted legacy request",
    );

    const totalTrialDays = 12;
    const warningDay = 10;


    const trialStart = new Date(trialStartDate);
    const trialEnd = warningRequest.mode === "send"
      ? new Date(warningRequest.trialEndDate)
      : new Date(trialStart);
    if (warningRequest.mode === "legacy") {
      trialEnd.setDate(trialEnd.getDate() + totalTrialDays);
    }


    const daysLeft = warningRequest.mode === "send"
      ? utcCalendarDaysUntil(trialEnd)
      : totalTrialDays - warningDay;


    const trialEndFormatted = trialEnd.toLocaleDateString("en-US", {
      year: "numeric",
      month: "long",
      day: "numeric",
    });


    const htmlContent = `
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Your Free Trial Is Ending Soon</title>
  <style>
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
  </style>
</head>
<body>
  <div class="email-container">
    <div class="logo-container">
      <div class="logo">NubrixAI<span>.</span></div>
    </div>
    
    <h2>⏳ Your Free Trial Is Ending Soon</h2>
    
    <p>
      Hi${name ? ` ${name}` : " there"}! We hope you've been getting incredible value out of Nubrix AI over the past several days.
    </p>

    <div class="warning-details">
      <p>
        Just a quick heads up: your free trial expires in <strong class="highlight">${daysLeft} days</strong> (on <strong>${trialEndFormatted}</strong>).
      </p>
    </div>

    <p>
      To prevent any interruption to your scheduled reports, custom charts, and active transformation threads, upgrade to one of our premium plans today. Your existing data and configurations will remain completely intact.
    </p>

    <div class="cta-container">
      <a href="${APP_URL}/billing" class="button">Upgrade Your Plan</a>
    </div>

    <p>
      No action is required if you choose not to subscribe. Your access will simply pause on the trial end date, and you won't be charged anything.
    </p>

    <div class="footer">
      <p>© 2026 ${SENDER_NAME}. All rights reserved.</p>
    </div>
  </div>
</body>
</html>
`;


    const providerPayload: Record<string, unknown> = {
      sender: {
        email: SENDER_EMAIL,
        name: SENDER_NAME,
      },
      to: [{ email, name: name || undefined }],
      subject: `⏳ Your Free Trial Ends in ${daysLeft} Days`,
      htmlContent,
    };
    if (warningRequest.mode === "send") {
      providerPayload.tags = [warningRequest.trackingTag];
      providerPayload.headers = {
        "X-Mailin-custom": `delivery_id:${warningRequest.deliveryId}`,
      };
    }

    let brevoRes: Response;
    try {
      brevoRes = await fetch("https://api.brevo.com/v3/smtp/email", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "api-key": BREVO_API_KEY!,
        },
        body: JSON.stringify(providerPayload),
        signal: AbortSignal.timeout(8_000),
      });
    } catch (error) {
      if (
        error instanceof DOMException &&
        (error.name === "TimeoutError" || error.name === "AbortError")
      ) {
        return errorResponse("BREVO_TIMEOUT", 504);
      }
      throw error;
    }


    const brevoBody = await brevoRes.text();
    console.log(`[warningEmail] Brevo response status=${brevoRes.status}`);


    if (!brevoRes.ok) {
      const responseStatus = brevoRes.status === 429
        ? 429
        : brevoRes.status >= 500
        ? 503
        : 422;
      return errorResponse(`BREVO_HTTP_${brevoRes.status}`, responseStatus);
    }


    if (warningRequest.mode === "legacy") {
      return jsonResponse({
        success: true,
        trialEndsOn: trialEndFormatted,
      });
    }

    let providerBody: Record<string, unknown> = {};
    try {
      providerBody = JSON.parse(brevoBody);
    } catch {
      return errorResponse("PROVIDER_RESPONSE_INVALID", 502);
    }
    return jsonResponse(acceptedResponse(String(providerBody.messageId ?? "")));


  } catch (err) {
    const errorCode = err instanceof Error ? err.message : "INTERNAL_ERROR";
    const clientErrors = new Set([
      "INVALID_REQUEST_BODY",
      "UNSUPPORTED_NOTIFICATION_TYPE",
      "UNSUPPORTED_TEMPLATE_VERSION",
      "INVALID_DELIVERY_ID",
      "INVALID_RECIPIENT",
      "INVALID_RECIPIENT_NAME",
      "INVALID_TRIAL_START",
      "INVALID_TRIAL_END",
      "INVALID_TRACKING_TAG",
    ]);
    if (clientErrors.has(errorCode)) {
      console.warn(`[warningEmail] Rejected request code=${errorCode}`);
      return errorResponse(errorCode, 400);
    }
    if (errorCode === "FUNCTION_NOT_CONFIGURED") {
      console.error("[warningEmail] Required function configuration is missing");
      return errorResponse(errorCode, 503);
    }
    if (errorCode === "PROVIDER_MESSAGE_ID_MISSING") {
      console.error("[warningEmail] Provider response omitted messageId");
      return errorResponse(errorCode, 502);
    }
    console.error("[warningEmail] Unexpected internal error");
    return errorResponse("INTERNAL_ERROR", 500);
  }
});
