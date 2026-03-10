#!/usr/bin/env node
/**
 * List available baggage services for an existing order and optionally add them.
 *
 * Usage:
 *   # List available baggage options for an order
 *   node add-baggage.mjs --order <order_id>
 *
 *   # Add baggage to an order
 *   node add-baggage.mjs --order <order_id> --services '[{"id":"svc_xxx","quantity":1}]'
 *
 * How it works:
 *   Duffel allows adding paid services (e.g. checked bags) to an existing order
 *   via POST /air/orders/{id}/services. Payment is taken immediately from Balance.
 *
 * Tip:
 *   Use check-flight.mjs --offer <id> before booking to see baggage options upfront
 *   and pass them via --services to save-booking.mjs at booking time (cheaper flow).
 */

import { duffelGet, duffelPost, parseArgs, output } from "./lib.mjs";

const args = parseArgs();

if (!args.order) {
  console.error("Usage: node add-baggage.mjs --order <order_id> [--services '<json>']");
  process.exit(1);
}

try {
  // Fetch order details + available services
  const orderResp = await duffelGet(`/air/orders/${args.order}`, { return_available_services: true });
  const order = orderResp.data;

  if (!order) {
    output({ error: "Order not found." });
    process.exit(1);
  }

  // Build passenger map for readable output
  const passengerMap = {};
  for (const p of order.passengers || []) {
    passengerMap[p.id] = `${p.given_name} ${p.family_name}`;
  }

  // Build segment map for readable output (outbound/return)
  const segmentMap = {};
  for (const [sliceIdx, slice] of (order.slices || []).entries()) {
    for (const seg of slice.segments || []) {
      segmentMap[seg.id] = `${seg.origin.iata_code}→${seg.destination.iata_code} (${sliceIdx === 0 ? "outbound" : "return"})`;
    }
  }

  // Filter baggage services
  const baggageServices = (order.available_services || []).filter(
    (svc) => svc.type === "baggage"
  );

  const formattedServices = baggageServices.map((svc) => ({
    id: svc.id,
    baggage_type: svc.metadata?.type || "checked",      // checked | carry_on
    maximum_weight_kg: svc.metadata?.maximum_weight_kg,
    maximum_length_cm: svc.metadata?.maximum_length_cm,
    price: `${svc.total_amount} ${svc.total_currency}`,
    price_raw: parseFloat(svc.total_amount),
    currency: svc.total_currency,
    maximum_quantity: svc.maximum_quantity,
    passengers: (svc.passenger_ids || []).map((id) => passengerMap[id] || id),
    segments: (svc.segment_ids || []).map((id) => segmentMap[id] || id),
  }));

  // If no --services flag, just list available options
  if (!args.services) {
    output({
      order_id: order.id,
      booking_reference: order.booking_reference,
      total_paid: `${order.total_amount} ${order.total_currency}`,
      available_baggage: formattedServices,
      tip: formattedServices.length > 0
        ? `Add bags with: node add-baggage.mjs --order ${order.id} --services '[{"id":"<service_id>","quantity":1}]'`
        : "No baggage services available for this order (may not be supported by airline or already included).",
    });
    process.exit(0);
  }

  // Parse and validate services to add
  let servicesToAdd;
  try {
    servicesToAdd = JSON.parse(args.services);
    if (!Array.isArray(servicesToAdd) || servicesToAdd.length === 0) {
      throw new Error("services must be a non-empty JSON array of {id, quantity}");
    }
  } catch (err) {
    output({ error: `Invalid services JSON: ${err.message}` });
    process.exit(1);
  }

  // Calculate total additional cost
  const totalExtra = servicesToAdd.reduce((sum, svc) => {
    const found = baggageServices.find((b) => b.id === svc.id);
    return sum + (found ? parseFloat(found.total_amount) * (svc.quantity || 1) : 0);
  }, 0);

  // Add services to order
  const result = await duffelPost(`/air/orders/${args.order}/services`, {
    data: {
      add: servicesToAdd.map((svc) => ({
        id: svc.id,
        quantity: svc.quantity || 1,
      })),
      payment: {
        type: process.env.DUFFEL_PAYMENT_TYPE || "balance",
        amount: totalExtra.toFixed(2),
        currency: order.total_currency,
      },
    },
  });

  const updatedOrder = result.data;

  output({
    status: "services_added",
    order_id: updatedOrder.id,
    booking_reference: updatedOrder.booking_reference,
    new_total: `${updatedOrder.total_amount} ${updatedOrder.total_currency}`,
    services_added: (updatedOrder.services || []).map((svc) => ({
      id: svc.id,
      type: svc.type,
      metadata: svc.metadata,
      passengers: (svc.passenger_ids || []).map((id) => passengerMap[id] || id),
      segments: (svc.segment_ids || []).map((id) => segmentMap[id] || id),
    })),
  });
} catch (err) {
  output({ error: err.message });
  process.exit(1);
}
