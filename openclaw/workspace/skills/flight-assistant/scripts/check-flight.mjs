#!/usr/bin/env node
/**
 * Get detailed offer information and verify it's still available.
 *
 * Usage:
 *   node check-flight.mjs --offer <offer_id>
 *
 * Retrieves the latest offer details including price, conditions, and available services (bags).
 * Use this before booking to confirm the price and see what baggage options are available.
 *
 * Baggage services are grouped by passenger and leg (outbound/return).
 * Pass service IDs to save-booking.mjs via --services to add them at booking time.
 */

import { duffelGet, parseArgs, formatDuration, formatDateTime, output } from "./lib.mjs";

const args = parseArgs();

if (!args.offer) {
  console.error("Usage: node check-flight.mjs --offer <offer_id>");
  process.exit(1);
}

try {
  const response = await duffelGet(`/air/offers/${args.offer}`, { return_available_services: true });
  const offer = response.data;

  if (!offer) {
    output({ status: "not_found", error: "Offer not found or expired." });
    process.exit(1);
  }

  const expired = new Date(offer.expires_at) < new Date();
  if (expired) {
    output({
      status: "expired",
      error: "This offer has expired. Please search again.",
      expired_at: offer.expires_at,
    });
    process.exit(1);
  }

  // Extract available services — group baggage by passenger and segment
  const allServices = offer.available_services || [];
  const baggage = allServices
    .filter((svc) => svc.type === "baggage")
    .map((svc) => ({
      id: svc.id,
      type: svc.type,
      total_amount: svc.total_amount,
      total_currency: svc.total_currency,
      maximum_quantity: svc.maximum_quantity,
      passenger_ids: svc.passenger_ids || [],
      segment_ids: svc.segment_ids || [],
      baggage_type: svc.metadata?.type,         // checked, carry_on
      maximum_weight_kg: svc.metadata?.maximum_weight_kg,
      maximum_length_cm: svc.metadata?.maximum_length_cm,
    }));

  const otherServices = allServices
    .filter((svc) => svc.type !== "baggage")
    .map((svc) => ({
      id: svc.id,
      type: svc.type,
      total_amount: svc.total_amount,
      total_currency: svc.total_currency,
      maximum_quantity: svc.maximum_quantity,
      metadata: svc.metadata,
    }));

  const slices = (offer.slices || []).map((slice) => {
    const segments = slice.segments || [];
    const first = segments[0];
    const last = segments[segments.length - 1];
    return {
      origin: first?.origin?.iata_code,
      destination: last?.destination?.iata_code,
      departure: formatDateTime(first?.departing_at),
      arrival: formatDateTime(last?.arriving_at),
      duration: formatDuration(slice.duration),
      stops: segments.length - 1,
      airlines: [...new Set(segments.map((s) => s.operating_carrier?.name || ""))].join(", "),
    };
  });

  output({
    status: "available",
    offer_id: offer.id,
    price: `${offer.total_amount} ${offer.total_currency}`,
    total_amount: offer.total_amount,
    total_currency: offer.total_currency,
    base_amount: offer.base_amount,
    tax_amount: offer.tax_amount,
    expires_at: offer.expires_at,
    owner: offer.owner?.name,
    slices,
    passengers: (offer.passengers || []).map((p) => ({
      id: p.id,
      type: p.type,
      age: p.age,
    })),
    conditions: {
      refundable: offer.conditions?.refund_before_departure?.allowed || false,
      changeable: offer.conditions?.change_before_departure?.allowed || false,
      refund_penalty: offer.conditions?.refund_before_departure?.penalty_amount,
      change_penalty: offer.conditions?.change_before_departure?.penalty_amount,
    },
    baggage_services: baggage,
    other_services: otherServices,
    payment_requirements: offer.payment_requirements,
  });
} catch (err) {
  output({ error: err.message });
  process.exit(1);
}
