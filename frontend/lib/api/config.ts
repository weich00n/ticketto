import { apiGatewayUrl } from "@/lib/auth/config";

// Read on the server at request time, so one build can run with or without the
// backend services. Demo data stays on until USE_DEMO_DATA=false and a gateway is set.
export const demoDataEnabled = () =>
  !apiGatewayUrl() || process.env.USE_DEMO_DATA !== "false";

// Public by design: the browser needs it to load Stripe's Payment Element.
export const stripePublishableKey = () =>
  process.env.STRIPE_PUBLISHABLE_KEY ?? "";
