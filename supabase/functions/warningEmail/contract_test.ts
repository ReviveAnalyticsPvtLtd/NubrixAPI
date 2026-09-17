import {
  acceptedResponse,
  assertRequiredConfiguration,
  parseWarningRequest,
  validationResponse,
} from "./contract.ts";


function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}


function assertEquals(actual: unknown, expected: unknown): void {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expected);
  assert(actualJson === expectedJson, `${actualJson} !== ${expectedJson}`);
}


function assertThrows(action: () => unknown, expectedCode: string): void {
  try {
    action();
  } catch (error) {
    assert(error instanceof Error, "expected Error instance");
    assert(error.message === expectedCode, `${error.message} !== ${expectedCode}`);
    return;
  }
  throw new Error(`expected ${expectedCode} to be thrown`);
}


Deno.test("validation mode requires no recipient and never becomes send mode", () => {
  const parsed = parseWarningRequest({
    mode: "validate",
    notificationType: "trial_expiry_warning",
    templateVersion: "1",
  });

  assertEquals(parsed, {
    mode: "validate",
    notificationType: "trial_expiry_warning",
    templateVersion: "1",
  });
  assertEquals(validationResponse(), {
    status: "ok",
    notificationTypes: ["trial_expiry_warning"],
    templateVersions: ["1"],
  });
});


Deno.test("durable send requires delivery identity and recipient", () => {
  assertThrows(() => parseWarningRequest({
    notificationType: "trial_expiry_warning",
    templateVersion: "1",
  }), "INVALID_DELIVERY_ID");
});


Deno.test("durable send accepts the complete versioned contract", () => {
  const parsed = parseWarningRequest({
    deliveryId: "11111111-1111-4111-8111-111111111111",
    notificationType: "trial_expiry_warning",
    templateVersion: "1",
    email: "recipient@example.test",
    name: "Recipient",
    trialStartDate: "2026-09-07T00:00:00+00:00",
    trialEndDate: "2026-09-19T00:00:00+00:00",
    trackingTag:
      "nubrix_delivery:11111111-1111-4111-8111-111111111111",
  });

  assert(parsed.mode === "send", "expected durable send mode");
  assert(parsed.deliveryId === "11111111-1111-4111-8111-111111111111", "wrong delivery id");
});


Deno.test("tracking tag must be derived from delivery id", () => {
  assertThrows(() => parseWarningRequest({
    deliveryId: "11111111-1111-4111-8111-111111111111",
    notificationType: "trial_expiry_warning",
    templateVersion: "1",
    email: "recipient@example.test",
    name: "Recipient",
    trialStartDate: "2026-09-07T00:00:00+00:00",
    trialEndDate: "2026-09-19T00:00:00+00:00",
    trackingTag: "unrelated-tag",
  }), "INVALID_TRACKING_TAG");
});


Deno.test("legacy payload remains supported during rollout", () => {
  const parsed = parseWarningRequest({
    email: "recipient@example.test",
    name: "Recipient",
    trialStartDate: "2026-09-07T00:00:00+00:00",
  });

  assert(parsed.mode === "legacy", "expected legacy mode");
});


Deno.test("provider acceptance requires a message id", () => {
  assertThrows(() => acceptedResponse(""), "PROVIDER_MESSAGE_ID_MISSING");
  assertEquals(acceptedResponse("<message@brevo>"), {
    status: "accepted",
    provider: "brevo",
    messageId: "<message@brevo>",
  });
});

Deno.test("validation rejects missing provider configuration", () => {
  assertThrows(
    () => assertRequiredConfiguration({
      brevoApiKey: "",
      senderEmail: "sender@example.test",
      senderName: "Nubrix",
      appUrl: "https://app.example.test",
    }),
    "FUNCTION_NOT_CONFIGURED",
  );
  assertRequiredConfiguration({
    brevoApiKey: "key",
    senderEmail: "sender@example.test",
    senderName: "Nubrix",
    appUrl: "https://app.example.test",
  });
});
