#!/usr/bin/env node
/**
 * Confirm payment for a held Duffel order.
 *
 * BALANCE MODE (default, fully automatic):
 *   node confirm-payment.mjs --order <order_id>
 *
 * CARD MODE (requires 3DS via browser):
 *   node confirm-payment.mjs --order <order_id> --card --3ds-session <session_id>
 *
 * The preferred flow is Balance (pre-funded account, no OTP needed).
 * Card payment requires creating a 3DS session via the Duffel dashboard
 * or Duffel's card form component, which needs browser interaction.
 */

import { duffelPost, duffelGet, parseArgs, output } from "./lib.mjs";

const PAYMENT_TYPE = process.env.DUFFEL_PAYMENT_TYPE || "balance";

const args = parseArgs();

if (!args.order) {
  console.error("Usage: node confirm-payment.mjs --order <order_id>");
  process.exit(1);
}

try {
  // Get the order to know current amount
  const orderResp = await duffelGet(`/air/orders/${args.order}`);
  const order = orderResp.data;

  if (!order) {
    output({ error: "Order not found." });
    process.exit(1);
  }

  // Check if already paid
  if (order.payment_status?.awaiting_payment === false) {
    output({
      status: "already_paid",
      order_id: order.id,
      booking_reference: order.booking_reference,
      message: "This order has already been paid.",
    });
    process.exit(0);
  }

  // Check payment deadline
  const deadline = order.payment_status?.payment_required_by;
  if (deadline && new Date(deadline) < new Date()) {
    output({
      status: "expired",
      error: `Payment deadline passed (${deadline}). Order may have been cancelled.`,
    });
    process.exit(1);
  }

  // Build payment request
  const paymentBody = {
    data: {
      order_id: args.order,
      payment: {
        type: args.card ? "card" : PAYMENT_TYPE,
        amount: order.total_amount,
        currency: order.total_currency,
      },
    },
  };

  // Add 3DS session for card payments
  if (args.card && args["3ds-session"]) {
    paymentBody.data.payment.three_d_secure_session_id = args["3ds-session"];
  }

  const result = await duffelPost("/air/payments", paymentBody);
  const payment = result.data;

  output({
    status: payment.status, // succeeded, failed, pending
    payment_id: payment.id,
    order_id: args.order,
    booking_reference: order.booking_reference,
    amount: `${payment.amount} ${payment.currency}`,
    type: payment.type,
    message:
      payment.status === "succeeded"
        ? `Payment confirmed! Booking reference: ${order.booking_reference}`
        : `Payment status: ${payment.status}${payment.failure_reason ? ` — ${payment.failure_reason}` : ""}`,
  });
} catch (err) {
  output({ error: err.message });
  process.exit(1);
}
