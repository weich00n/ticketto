# End-to-end tests

Playwright drives a real browser against the deployed app and the real backend.

| Spec                           | Scenario                                                                                                                                                                                                                                                                                               |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `s1-organiser-publishes-event` | An organiser creates an event with a category and an image, is refused a release less than 15 minutes away with the reason, then publishes it with the earliest allowed time; the category is kept and Reservation lists it as on sale                                                                 |
| `s2-s3-buyer-journey`          | A buyer queues, holds and pays with Stripe, and stock drops by one; a declined card can be retried on the same order; the buyer gets a ticket that the organiser checks in once; then the organiser cancels an event, Reservation stops selling it, and the buyer's ticket is invalidated and refunded |

## Run

```sh
AWS_PROFILE=ticketto npm run e2e                 # all specs
AWS_PROFILE=ticketto npx playwright test s1      # one spec
E2E_BASE_URL=http://localhost:3000 AWS_PROFILE=ticketto npm run e2e
npx playwright show-report                       # last run, with video
```

The app's URL defaults to the deployed site. `COGNITO_USER_POOL_ID` and `AWS_REGION`
come from `.env.local`, and the AWS profile needs permission to create and delete users
in that pool.

## What the tests create

Every run makes its own Cognito users, publishes events and pays with Stripe's test card
(`4242 4242 4242 4242`). The users are deleted and the events cancelled when the test ends,
even if it fails. A failed run can leave a cancelled event behind.

The purchase scenario takes about 20 minutes: the queue admits buyers at the release
time, and an event can only be published when its release is at least 15 minutes away.
Run one run at a time; they share a backend and one `test-results` folder.

## Last full run

Both specs passed against the deployed site on 1 November 2026 (S1 in 30 s, S2 and S3 in
17.0 min). The buyer queued, held a ticket, paid with the test card, got a valid ticket
and QR, and the organiser's check-in was accepted once and rejected the second time.
After the organiser cancelled the event, the ticket was invalidated and Checkout
recorded a refund.
