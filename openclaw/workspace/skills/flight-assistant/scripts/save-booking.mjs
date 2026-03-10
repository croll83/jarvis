#!/usr/bin/env node
/**
 * Book a flight via Duffel Orders API.
 *
 * For instant booking with balance payment (fully automatic):
 *   node save-booking.mjs --offer <offer_id> --passengers '<json>' [--type instant]
 *
 * For hold booking (pay later):
 *   node save-booking.mjs --offer <offer_id> --passengers '<json>' --type hold
 *
 * Passenger JSON format (array):
 *   [{
 *     "id": "<passenger_id from offer>",
 *     "given_name": "Marco",
 *     "family_name": "Monaco",
 *     "born_on": "1990-01-15",
 *     "email": "marco@example.com",
 *     "phone_number": "+39123456789",
 *     "title": "mr",
 *     "gender": "m",
 *     "passport_number": "AB1234567",
 *     "passport_expiry": "2030-01-15",
 *     "passport_country": "IT"
 *   }]
 *
 * Options:
 *   --services <json>   Additional services to add (bags, seats) as JSON array of {id, quantity}
 *   --currency <code>   Currency for payment (default: from offer)
 */

import { duffelPost, duffelGet, parseArgs, output } from "./lib.mjs";

const PAYMENT_TYPE = process.env.DUFFEL_PAYMENT_TYPE || "balance";
const CONTACT_EMAIL = process.env.DUFFEL_EMAIL || "";
const CONTACT_PHONE = process.env.DUFFEL_PHONE || "";

const args = parseArgs();

if (!args.offer || !args.passengers) {
  console.error("Usage: node save-booking.mjs --offer <offer_id> --passengers '<json>'");
  process.exit(1);
}

let passengers;
try {
  passengers = JSON.parse(args.passengers);
  if (!Array.isArray(passengers) || passengers.length === 0) {
    throw new Error("passengers must be a non-empty JSON array");
  }
} catch (err) {
  console.error(`Invalid passengers JSON: ${err.message}`);
  process.exit(1);
}

// Build Duffel passenger objects
const apiPassengers = passengers.map((p) => {
  const pax = {
    id: p.id, // must match passenger id from the offer
    given_name: p.given_name || p.name,
    family_name: p.family_name || p.surname,
    born_on: p.born_on || p.birthday,
    email: p.email || CONTACT_EMAIL,
    phone_number: p.phone_number || p.phone || CONTACT_PHONE,
    title: p.title || "mr",
    gender: p.gender || "m",
  };

  // Associate infant to accompanying adult
  // Set infant_passenger_id on the adult passenger to link them
  if (p.infant_passenger_id) {
    pax.infant_passenger_id = p.infant_passenger_id;
  }

  // Add identity document (passport) if provided
  if (p.passport_number) {
    pax.identity_documents = [
      {
        type: "passport",
        unique_identifier: p.passport_number,
        expires_on: p.passport_expiry,
        issuing_country_code: p.passport_country || p.nationality,
      },
    ];
  }

  return pax;
});

const orderType = args.type || "instant";

try {
  // First, get the offer to know the price (with services for accurate total)
  const offerResp = await duffelGet(`/air/offers/${args.offer}`, { return_available_services: true });
  const offer = offerResp.data;

  if (!offer) {
    output({ error: "Offer not found or expired." });
    process.exit(1);
  }

  // Parse services if specified
  let selectedServices = [];
  if (args.services) {
    try {
      selectedServices = JSON.parse(args.services);
    } catch {
      console.error("Invalid services JSON");
      process.exit(1);
    }
  }

  // Calculate total including services
  let totalAmount = parseFloat(offer.total_amount);
  const availableServices = offer.available_services || [];
  for (const sel of selectedServices) {
    const svc = availableServices.find((s) => s.id === sel.id);
    if (svc) {
      totalAmount += parseFloat(svc.total_amount) * (sel.quantity || 1);
    }
  }

  // Build order request
  const orderBody = {
    data: {
      type: orderType,
      selected_offers: [args.offer],
      passengers: apiPassengers,
    },
  };

  // For instant booking, include payment with correct total
  if (orderType === "instant") {
    orderBody.data.payments = [
      {
        type: PAYMENT_TYPE,
        amount: totalAmount.toFixed(2),
        currency: offer.total_currency,
      },
    ];
  }

  // Add services to order if any
  if (selectedServices.length > 0) {
    orderBody.data.services = selectedServices;
  }

  const result = await duffelPost("/air/orders", orderBody);
  const order = result.data;

  output({
    status: orderType === "instant" ? "booked_and_paid" : "held",
    order_id: order.id,
    booking_reference: order.booking_reference,
    total_amount: order.total_amount,
    total_currency: order.total_currency,
    payment_status: order.payment_status,
    created_at: order.created_at,
    slices: (order.slices || []).map((slice) => {
      const segs = slice.segments || [];
      return {
        origin: segs[0]?.origin?.iata_code,
        destination: segs[segs.length - 1]?.destination?.iata_code,
        departure: segs[0]?.departing_at,
        arrival: segs[segs.length - 1]?.arriving_at,
        airline: segs[0]?.operating_carrier?.name,
      };
    }),
    passengers: (order.passengers || []).map((p) => ({
      given_name: p.given_name,
      family_name: p.family_name,
    })),
    next_step:
      orderType === "hold"
        ? `Pay before ${order.payment_status?.payment_required_by || "deadline"} using: node confirm-payment.mjs --order ${order.id}`
        : "Booking complete! Save the booking_reference for check-in.",
  });
} catch (err) {
  output({ error: err.message });
  process.exit(1);
}
