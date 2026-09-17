export const NOTIFICATION_TYPE = "trial_expiry_warning" as const;
export const TEMPLATE_VERSION = "1" as const;


export type ValidationWarningRequest = {
  mode: "validate";
  notificationType: typeof NOTIFICATION_TYPE;
  templateVersion: typeof TEMPLATE_VERSION;
};


export type LegacyWarningRequest = {
  mode: "legacy";
  email: string;
  name: string;
  trialStartDate: string;
};


export type DurableWarningRequest = {
  mode: "send";
  deliveryId: string;
  notificationType: typeof NOTIFICATION_TYPE;
  templateVersion: typeof TEMPLATE_VERSION;
  email: string;
  name: string;
  trialStartDate: string;
  trialEndDate: string;
  trackingTag: string;
};


export type WarningRequest =
  | ValidationWarningRequest
  | LegacyWarningRequest
  | DurableWarningRequest;


export type WarningEmailConfiguration = {
  brevoApiKey?: string;
  senderEmail?: string;
  senderName?: string;
  appUrl?: string;
};


export function assertRequiredConfiguration(
  configuration: WarningEmailConfiguration,
): void {
  if (
    !configuration.brevoApiKey ||
    !configuration.senderEmail ||
    !configuration.senderName ||
    !configuration.appUrl
  ) {
    throw new Error("FUNCTION_NOT_CONFIGURED");
  }
}


function asRecord(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("INVALID_REQUEST_BODY");
  }
  return value as Record<string, unknown>;
}


function requiredString(
  body: Record<string, unknown>,
  field: string,
  errorCode: string,
): string {
  const value = String(body[field] ?? "").trim();
  if (!value) throw new Error(errorCode);
  return value;
}


function validDate(value: string, errorCode: string): string {
  if (Number.isNaN(new Date(value).getTime())) throw new Error(errorCode);
  return value;
}


function validateVersion(body: Record<string, unknown>): void {
  if (body.notificationType !== NOTIFICATION_TYPE) {
    throw new Error("UNSUPPORTED_NOTIFICATION_TYPE");
  }
  if (String(body.templateVersion ?? "") !== TEMPLATE_VERSION) {
    throw new Error("UNSUPPORTED_TEMPLATE_VERSION");
  }
}


export function parseWarningRequest(input: unknown): WarningRequest {
  const body = asRecord(input);

  if (body.mode === "validate") {
    validateVersion(body);
    return {
      mode: "validate",
      notificationType: NOTIFICATION_TYPE,
      templateVersion: TEMPLATE_VERSION,
    };
  }

  const isLegacy = body.deliveryId === undefined &&
    body.notificationType === undefined && body.templateVersion === undefined;
  if (isLegacy) {
    const email = requiredString(body, "email", "INVALID_RECIPIENT");
    const trialStartDate = validDate(
      requiredString(body, "trialStartDate", "INVALID_TRIAL_START"),
      "INVALID_TRIAL_START",
    );
    return {
      mode: "legacy",
      email,
      name: String(body.name ?? "").trim(),
      trialStartDate,
    };
  }

  validateVersion(body);
  const deliveryId = requiredString(body, "deliveryId", "INVALID_DELIVERY_ID");
  const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  if (!uuidV4.test(deliveryId)) throw new Error("INVALID_DELIVERY_ID");

  const email = requiredString(body, "email", "INVALID_RECIPIENT");
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    throw new Error("INVALID_RECIPIENT");
  }
  const name = requiredString(body, "name", "INVALID_RECIPIENT_NAME");
  const trialStartDate = validDate(
    requiredString(body, "trialStartDate", "INVALID_TRIAL_START"),
    "INVALID_TRIAL_START",
  );
  const trialEndDate = validDate(
    requiredString(body, "trialEndDate", "INVALID_TRIAL_END"),
    "INVALID_TRIAL_END",
  );
  if (new Date(trialEndDate) <= new Date(trialStartDate)) {
    throw new Error("INVALID_TRIAL_END");
  }

  const trackingTag = requiredString(
    body,
    "trackingTag",
    "INVALID_TRACKING_TAG",
  );
  if (trackingTag !== `nubrix_delivery:${deliveryId}`) {
    throw new Error("INVALID_TRACKING_TAG");
  }

  return {
    mode: "send",
    deliveryId,
    notificationType: NOTIFICATION_TYPE,
    templateVersion: TEMPLATE_VERSION,
    email,
    name,
    trialStartDate,
    trialEndDate,
    trackingTag,
  };
}


export function validationResponse() {
  return {
    status: "ok",
    notificationTypes: [NOTIFICATION_TYPE],
    templateVersions: [TEMPLATE_VERSION],
  };
}


export function acceptedResponse(messageId: string) {
  const normalized = String(messageId ?? "").trim();
  if (!normalized) throw new Error("PROVIDER_MESSAGE_ID_MISSING");
  return {
    status: "accepted",
    provider: "brevo",
    messageId: normalized,
  };
}
