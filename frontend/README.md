# Ticketto — Frontend

The independently runnable web UI for the event-ticketing platform. Built with Next.js 16 App Router, React 19, TypeScript, and Tailwind CSS 4.

This folder owns presentation and browser interactions. Backend microservices live in sibling folders and have their own dependencies, configuration, data, and deployment lifecycle.

## Getting started

Use Node.js 20.9 or newer and npm. From this folder:

```sh
npm ci
npm run dev
```

Open [localhost:3000](http://localhost:3000). Browsing events works with the demo data. Authentication requires Cognito configuration; start with `.env.example` when connecting a local emulator or AWS.

For a production build:

```sh
npm run build
npm start
```

## Project structure

```text
cs302-2026/g2-team5
├── frontend/                     # This application
│   ├── app/                      # Routes, the (platform) app-shell layout, global CSS, loading/error UI
│   ├── components/
│   │   ├── layout/platform.tsx    # App shell, screen selection, demo interaction wiring
│   │   └── ui/                   # Shared controls, animations, and presentation helpers
│   ├── features/
│   │   ├── auth/                 # Login, sign-up, and email-confirmation UI
│   │   ├── events/               # Home, browse, event details, and category labels
│   │   ├── booking/              # Queue, selection, checkout, and ticket screens
│   │   └── organizer/            # Dashboard, event details, and event form
│   ├── lib/
│   │   ├── auth/                 # Server-only Cognito, cookie, and validation helpers
│   │   ├── demo/                 # Sample events and browser persistence hook
│   │   ├── format.ts             # Currency and date formatting
│   │   └── utils.ts              # CSS class merging
│   ├── types/ticketing.ts         # Frontend domain models
│   └── public/                   # Used event images and application icon
└──  backend/                     # Backend services
    ├── user-service/
    ├── event-service/
    ├── queue-service/
    ├── reservation-service/
    ├── checkout-service/
    └── ticket-service/
```

Add future services beside `frontend`, not inside it. Feature folders group UI concerns; they do not define backend service boundaries.

## Screens

| URL                               | Screen                                            |
| --------------------------------- | ------------------------------------------------- |
| `/`                               | Home and featured events                          |
| `/login`                          | Cognito sign-in                                   |
| `/signup`                         | Account registration                              |
| `/confirm`                        | Email confirmation                                |
| `/events`                         | Search, category, and month filters; saved events |
| `/events/:eventId`                | Event details                                     |
| `/queue/:eventId`                 | Simulated waiting room                            |
| `/events/:eventId/tickets`        | Ticket selection                                  |
| `/checkout`                       | Demo checkout                                     |
| `/checkout/success`               | Order confirmation                                |
| `/tickets`                        | Buyer tickets                                     |
| `/tickets/:ticketId`              | Digital ticket                                    |
| `/organizer`                      | Organizer dashboard                               |
| `/organizer/events/new`           | Create an event                                   |
| `/organizer/events/:eventId`      | Organizer event details                           |
| `/organizer/events/:eventId/edit` | Edit an event                                     |

`app/(platform)/layout.tsx` renders `components/layout/platform.tsx`, which selects the screen using the pathname. Because it is a layout, it keeps its state (notifications, loaded events) between screens. `app/(platform)/page.tsx` and `app/(platform)/[...slug]/page.tsx` only claim the URLs and render nothing themselves. Exact auth routes and API route handlers take precedence over this catch-all route. Unknown paths currently display an empty-state screen rather than an HTTP 404.

## Authentication and gateway boundary

The frontend uses the direct Cognito API flow described in the team setup notes:

- `POST /api/auth/signup`, `/confirm`, `/resend`, `/login`, `/refresh`, and `/logout` run on the Next.js server. They call Cognito through the AWS SDK, so passwords and Cognito secrets never reach browser JavaScript.
- Tokens are stored in HTTP-only, `SameSite=Lax` cookies that last as long as the refresh token (30 days). Cookies are `Secure` in production.
- `proxy.ts` guards ticket, checkout, and organiser screens. An expired access token is refreshed on the server; without a valid refresh token the visitor is sent to `/login?next=…` and returned there after signing in. This only improves the UI; API Gateway and services remain responsible for real authorization.
- Organiser screens need the `organiser` group in the ID token. Other signed-in users see an upgrade screen that calls `POST /api/v1/users/me/organiser` and then refreshes their tokens.
- `/api/proxy/:path*` forwards browser requests to `API_GATEWAY_URL`, adding the access-token bearer header when the visitor is signed in. Requests without a session are forwarded without one, so the gateway decides which routes are public. An expired token is refreshed before the request, and a 401 triggers one refresh and retry. Each request carries an `X-Correlation-Id` (created if the browser did not send one), and upstream calls time out after 29 seconds with a 504.
- On login, the frontend calls `POST /api/v1/users/me` through the configured gateway URL to initialise the profile. That call is skipped while `API_GATEWAY_URL` is unset.

### Connecting to the AWS user pool

Copy `.env.example` to `.env.local` and fill in values from the AWS console (Cognito → User pools → your pool):

| Variable                | Where to find it                                               |
| ----------------------- | -------------------------------------------------------------- |
| `AWS_REGION`            | The region in the pool ID prefix, for example `ap-southeast-1` |
| `COGNITO_USER_POOL_ID`  | Pool overview → User pool ID                                   |
| `COGNITO_CLIENT_ID`     | App integration → App clients → Client ID                      |
| `COGNITO_CLIENT_SECRET` | Same app client, only if it has a client secret                |
| `COGNITO_ENDPOINT`      | Leave empty for AWS                                            |
| `API_GATEWAY_URL`       | API Gateway invoke URL, without a trailing slash               |
| `USE_DEMO_DATA`         | `false` to load events from the gateway instead of sample data |

The pool must use email as the sign-in identifier and have `name` and `email` attributes, and an `organiser` group. No AWS access keys are needed: the frontend only calls Cognito's public sign-up and sign-in APIs.

Do not commit `.env.local`, the Cognito app-client secret, access tokens, or refresh tokens. The real app client must enable `ALLOW_USER_PASSWORD_AUTH` and `ALLOW_REFRESH_TOKEN_AUTH`; the local emulator uses `COGNITO_ENDPOINT` and has no client secret.

## Demo behavior

- `lib/demo/events.ts` supplies sample events.
- `lib/demo/use-demo-store.ts` persists events, orders, tickets, saved events, and queue admission under `ticketto-demo-v1` in localStorage.
- Queue positions use `ticketto-queue-<eventId>` keys. Remove those keys and `ticketto-demo-v1` in browser developer tools to reset the demo.
- Queue progression, reservations, payment, QR codes, notifications, and refunds are simulations. The Cognito session protects the frontend navigation, but demo event and ticket data stays in the browser. See [Connecting microservices](#connecting-microservices) to use the services instead.
- State belongs to one browser origin and is not shared between users. Demo checkout data is stored locally; use fictional details.
- Fonts load from Google Fonts, with fallback fonts when unavailable.

## Connecting microservices

With `API_GATEWAY_URL` set and `USE_DEMO_DATA=false`, every screen reads and writes through the gateway. Without them, the app runs on the demo data above.

Each service has its own adapter, which calls `api()` from `lib/api/client.ts` and maps responses to the types in `types/ticketing.ts`:

| Adapter                                | Service     | Gateway paths the frontend calls                                               |
| -------------------------------------- | ----------- | ------------------------------------------------------------------------------ |
| `features/events/api.ts`               | Event       | `GET /api/v1/events`, `GET /api/v1/events/:id`                                 |
| `features/organizer/api.ts`            | Event, User | `/api/v1/organizer/events/*`, `POST /api/v1/users/me/organiser`                |
| `features/booking/queue-api.ts`        | Queue       | `/api/v1/events/:id/queue/entries`, `/api/v1/events/:id/queue/me`              |
| `features/booking/reservations-api.ts` | Reservation | `GET /api/v1/events/:id/availability`, `/api/v1/reservations/*`                |
| `features/booking/checkout-api.ts`     | Checkout    | `/api/v1/orders/*` (the service itself serves `/orders`)                       |
| `features/booking/tickets-api.ts`      | Ticket      | `GET /api/v1/tickets`, `GET /api/v1/tickets/:id`, `GET /api/v1/tickets/:id/qr` |

The screens that use them are in `features/booking/components/live.tsx` and `features/organizer/components/live.tsx`; `components/layout/live-screens.tsx` picks them by URL. They reuse the demo screens' presentation.

- `api()` goes through `/api/proxy`, which attaches the user's access token; browser JavaScript cannot read it. Failed requests throw an `ApiError` with the service's message, and a 401 sends the visitor to `/login` unless `redirectOnUnauthorized: false` is passed.
- The proxy forwards only `Accept`, `Content-Type` and `Idempotency-Key` from the browser. The gateway must set `X-User-Sub`, `X-User-Username`, `X-User-Groups` and `X-User-Id` from the verified token on every route, overwriting anything a caller sends, because the services trust those headers.
- The admission token from the Queue Service and the booking in progress are kept in `sessionStorage` for the tab (`features/booking/booking-session.ts`), so a buyer can move between queue, selection, checkout and confirmation. The services remain the source of truth.
- The backend is authoritative for organiser permissions, queue admission, inventory, reservation expiry, prices, payment and ticket issuance. After `POST /api/v1/users/me/organiser`, the frontend refreshes its tokens and uses the new group claim; the User Service remains the source of truth for the role.

Not provided by a service yet, so shown neutrally or hidden in live mode: event subtitle, category, image and organiser name; seat numbers (tickets show their number in the order); organiser sales figures; and the booking fee.

The frontend does not contain any real API URL, app-client ID, app-client secret, or AWS credential.

## Development checks

```sh
npm run lint          # ESLint, including React and Next.js checks
npm run typecheck     # Generate route types and check TypeScript
npm run format:check # Check formatting
npm run format       # Apply formatting
npm run build        # Compile and validate the production application
```

There is no automated browser test suite yet. After changing an interaction, check event filtering, ticket selection and checkout, ticket persistence after refresh, organizer create/edit/cancel, and mobile navigation. A build alone does not verify these flows.

Before changing Next.js-specific code, read the installed guides in `node_modules/next/dist/docs/`.
