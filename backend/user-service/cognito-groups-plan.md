# Plan: get Cognito groups to the services through API Gateway

**Status:** Phases 0–3 done, **Phase 4 next** (needs the change window) · **Written** 2026-10-26 · **Affects** every service behind the shared HTTP API `api` (`x0vxfeenrl`) and the shared Cognito pool `us-east-1_2g8EYCEa1`

## Progress

| Phase | Status |
|---|---|
| 0. Team agreement | ✅ 2026-10-26: T1–T7 as proposed in §3 |
| 1. Code and unit tests | ✅ `cognito_triggers/pre_token_generation.py`, 13 tests in `tests/unit/test_pre_token_generation.py`, sample events in `tests/events/pre_token_generation/`. ruff and pylint clean (pylint CI job now includes `cognito_triggers`) |
| 2. Deploy the Lambda, not attached | ✅ `deploy/terraform/cognito.tf`, applied with `-target` (6 resources). The worker's pending changes were left alone: they need rabbitmq-service's secret first |
| 3. Test the function | ✅ Direct invokes: `organiser`, `organiser,staff`, `""`. **Gate passed** on a throwaway pool + API (deleted afterwards): Cognito accepts the empty claim, and API Gateway forwards `X-User-Groups: ""` with a 200. Forged headers are overwritten |
| T6 `staff` group | ✅ Created (empty). Still to decide: who adds people to it |
| 4. Attach the trigger | ⏳ In the change window, in the console (§5 Phase 4) |
| 5–8 | ⏳ |

Seen during testing: when Cognito couldn't invoke the trigger (a missing permission on the throwaway pool), **sign-in failed** with `UnexpectedLambdaException`. That confirms R1: a broken trigger blocks logins.

## 1. The problem

Services learn who the caller is from headers that API Gateway sets after it verifies the Cognito access token:

| Header | Comes from token claim | Works today? |
|---|---|---|
| `X-User-Sub` | `sub` | ✅ |
| `X-User-Username` | `username` | ✅ |
| `X-User-Groups` | `cognito:groups` | ❌ |

HTTP APIs **can't map a claim whose name contains a colon**. `$context.authorizer.claims.cognito:groups` is rejected with *"Invalid mapping expression specified"*. On 2026-10-25 we tested four spellings (`.cognito:groups`, `${…}`, `['cognito:groups']`, `cognito\:groups`) and all were rejected. Cognito always names the claim `cognito:groups`, and it can't be renamed.

**Who is affected**

| Service | Reads `X-User-Groups` for | Effect today |
|---|---|---|
| user-service | `token_refresh_required` hint after becoming an organiser | Minor: the hint is always `true`. The header is stripped (`remove:header.X-User-Groups`) so it can't be forged |
| event-service | Only `organiser` may create or manage events | **Blocking:** behind the gateway, every organiser is refused (403) |
| ticket-service | Only `staff` may check tickets in | **Blocking:** check-in is impossible. Also, no `staff` group exists in the pool yet |
| reservation-service | Defines the header name, doesn't use it | None |
| checkout, queue | Don't read it | None |

## 2. The fix

Add a **pre token generation Lambda trigger** to the Cognito pool. Cognito calls it every time it issues tokens (login and refresh). The Lambda adds a second, colon-free claim holding the same groups:

```
cognito:groups: ["organiser", "staff"]   ← unchanged, API Gateway can't read it
groups:         "organiser,staff"        ← new, API Gateway can
```

Each service's API Gateway integration then sets:

```hcl
"overwrite:header.X-User-Groups" = "$context.authorizer.claims.groups"
```

We tested that API Gateway accepts this mapping (2026-10-25).

```
Login / refresh                                    Every API request (unchanged path)
Browser ─► Cognito ─► Lambda: add groups claim     Browser ─token─► API Gateway (JWT check)
              ◄── access token with `groups` ──       │ X-User-Sub / -Username / -Groups ← claims
                                                       ▼ VPC link ─► service
```

**Why this option:**

| Option | Verdict |
|---|---|
| **Pre token generation Lambda** | ✅ Small, runs only at login and refresh (not per request), keeps the header contract every service already uses. The pool's **Essentials** tier supports customizing access tokens |
| Lambda *authorizer* instead of the JWT authorizer | ❌ Runs on every request (latency, cost), replaces the shared authorizer, and we'd have to write the JWT verification ourselves |
| Each service decodes the JWT itself | ❌ Changes every service and duplicates security-sensitive code. Breaks the agreed header contract |
| Switch to a REST API gateway | ❌ Rebuilds the shared gateway for one field |

## 3. Decisions for the team (before any work)

| | Question | Proposal |
|---|---|---|
| T1 | Who owns the Lambda? | **user-service.** It already owns the Cognito integration (profile creation, the organiser upgrade), and its OpenTofu can hold the Lambda |
| T2 | Claim name | **`groups`.** Not a Cognito-reserved name |
| T3 | Value format | **Comma-separated string**, e.g. `organiser,staff`, sorted. The parsers in user-service, event-service and ticket-service all accept it today, with no code changes |
| T4 | Value when the user is in no group (most buyers) | **Empty string `""`**, so the claim is always present. This must be tested (Phase 3); see risk R2 |
| T5 | Add to the ID token too? | **Yes.** Same value in both tokens, so the frontend can use it if it wants |
| T6 | The `staff` group | Create it now (ticket-service needs it), and decide who adds people to it |
| T7 | Change window | Attaching the trigger affects **every login on the platform** immediately. Pick a time, and tell the team |

## 4. What gets built

All in user-service's OpenTofu (`deploy/terraform/`), except the trigger attachment (Phase 4, see why there).

| Resource | Name | Notes |
|---|---|---|
| Lambda function | `cognito-pre-token-generation` | Python 3.13, 128 MB, **3 s timeout** (Cognito gives up after 5 s), no VPC, no dependencies |
| IAM role | `cognito-pre-token-generation` | `AWSLambdaBasicExecutionRole` only (write its own logs). It calls nothing else |
| Log group | `/aws/lambda/cognito-pre-token-generation` | 30 days |
| Lambda permission | — | Lets `cognito-idp.amazonaws.com` invoke it, **only from this pool** (`source_arn`) |
| CloudWatch alarm | `cognito-pre-token-generation-errors` | `Errors > 0` over 5 minutes. A failing trigger blocks logins (R1) |
| Provider | `hashicorp/archive` | Zips the code (`archive_file`) |

**The Lambda** (`cognito_triggers/pre_token_generation.py`, about 20 lines):

```python
def handler(event, context):
    """Copy cognito:groups into a colon-free `groups` claim that API Gateway can map."""
    groups = event["request"].get("groupConfiguration", {}).get("groupsToOverride") or []
    claims = {"groups": ",".join(sorted(groups))}
    event["response"]["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {"claimsToAddOrOverride": claims},
        "idTokenGeneration": {"claimsToAddOrOverride": claims},
    }
    return event
```

It must not raise for any input: missing `groupConfiguration`, `None`, or an empty list all give `groups: ""`.

## 5. Phases

Each phase is safe to stop after. Nothing changes for users until **Phase 4**.

### Phase 0: Team agreement
Agree T1–T7. Record them in the Technical Contract: `X-User-Groups` = comma-separated group names, may be empty, always set by API Gateway, never trusted from the client.

### Phase 1: Code and unit tests (no AWS)
- Add `cognito_triggers/pre_token_generation.py` and `tests/unit/test_pre_token_generation.py` in user-service. Test these cases:
  - one group;
  - several groups (check the sorted, comma-joined output);
  - no groups (`[]`, `None`, or `groupConfiguration` missing → `""`);
  - the rest of the event is returned unchanged.
- Run ruff, pylint and the tests. The existing CI covers it.

### Phase 2: Deploy the Lambda, not attached yet
- Add the §4 resources to `deploy/terraform/` and run `tofu plan` (expect only additions) and `tofu apply`.
- **No effect on anyone yet:** nothing calls the function.

### Phase 3: Test the function directly
Invoke it with sample Cognito V2 events and check the output:
```bash
aws lambda invoke --function-name cognito-pre-token-generation \
  --cli-binary-format raw-in-base64-out --payload file://tests/events/organiser.json out.json && jq . out.json
```
Cases: organiser, organiser + staff, no groups. All must return `groups` and no error.

**Gate:** confirm Cognito accepts an **empty-string** claim value (T4). If it doesn't, change the no-group value (e.g. omit the claim) and re-check in Phase 6 how API Gateway handles a missing claim. Buyers are most users, so this case must work.

### Phase 4: Attach the trigger (in the change window)
**Use the console, not the CLI or OpenTofu:**
- `aws cognito-idp update-user-pool` **resets every setting you don't pass to its default**. One incomplete command could wipe the pool's configuration for the whole team.
- OpenTofu can only attach triggers by managing the **entire** pool resource, which no single service should own.

Steps:
1. Cognito → User pools → `User pool - ticketto` → **Extensions** → **Add Lambda trigger**.
2. Trigger type **Authentication**, then **Pre token generation trigger**.
3. Event version: **Basic features + access token customization for user identities** (V2_0). V1 can't change the access token, and the frontend sends the access token.
4. Function: `cognito-pre-token-generation`. Save.

**Immediately afterwards:** log in with a test account. If login fails, **remove the trigger** (same page, Delete) and logins return to normal at once.

### Phase 5: Check the tokens
Log in (the login block from user-service `plan.md` §6), then decode the access token:
```bash
python3 -c 'import sys,json,base64;p=sys.argv[1].split(".")[1];print(json.dumps(json.loads(base64.urlsafe_b64decode(p+"="*(-len(p)%4))),indent=2))' "$TOKEN"
```
Expect both `"cognito:groups": ["organiser"]` and `"groups": "organiser"`. Repeat with a user in no group (`"groups": ""`).

### Phase 6: Switch user-service to the new claim
- In `deploy/terraform/main.tf`, change `"remove:header.X-User-Groups" = "''"` to `"overwrite:header.X-User-Groups" = "$context.authorizer.claims.groups"`, then `tofu apply`.
- Test through the gateway:
  - an organiser calls `POST /api/v1/users/me/organiser` → `token_refresh_required: false`;
  - a buyer logs in, gets promoted, and the response says `token_refresh_required: true`; after the next login it says `false`;
  - a user with **no groups**: `GET /api/v1/users/me` → **200** (not a 500 from API Gateway);
  - **forgery:** a buyer's token plus `X-User-Groups: organiser` → still treated as a buyer (`overwrite:` replaces it).

### Phase 7: Other services
Each owner adds the same line to **their** API Gateway integration when they add their routes:
```hcl
"overwrite:header.X-User-Sub"      = "$context.authorizer.claims.sub"
"overwrite:header.X-User-Username" = "$context.authorizer.claims.username"
"overwrite:header.X-User-Groups"   = "$context.authorizer.claims.groups"
```
- **event-service:** test that an organiser can create an event, a buyer gets 403, and a forged header gets 403.
- **ticket-service:** after the `staff` group exists (T6), test that staff can check in and others get 403.
- No code changes are needed in any service (T3).

### Phase 8: Docs
- Technical Contract: the header format (Phase 0).
- user-service `plan.md`: tick off the follow-up. `deploy/terraform/main.tf`: update the comment above the integration.
- Local development is unchanged. Services' tests set `X-User-*` headers directly and don't use the trigger.

## 6. Rollback

| Phase | Undo |
|---|---|
| 1–3 | Revert the code, or `tofu destroy -target` the Lambda resources. Nobody is affected |
| 4 | Console → Extensions → **delete the trigger**. Takes effect on the next login. New tokens lose `groups`; existing ones expire within 60 min |
| 6–7 | Put back `"remove:header.X-User-Groups" = "''"` and `tofu apply`. Services fall back to "no groups" |

**Order matters:** if the trigger is removed, revert Phase 6 and 7 first, or at the same time. Otherwise new tokens arrive without the claim, and API Gateway's behaviour with a missing claim is untested.

## 7. Risks

| | Risk | Mitigation |
|---|---|---|
| R1 | **If the Lambda errors or times out, Cognito refuses the login: for every user, on every service** | No dependencies, no network calls, can't raise (Phase 1 tests). 3 s timeout. Error alarm. Rollback is one click (Phase 4) |
| R2 | Users with no groups (most buyers): Cognito might reject an empty claim, or API Gateway might fail on it | Gate in Phase 3, test in Phase 6 before other services switch |
| R3 | Tokens issued before the change lack `groups` for up to **60 minutes** (access token lifetime) | Harmless: until Phase 6 nobody reads it. After Phase 6, `X-User-Groups` is empty, as it is today |
| R4 | Someone later edits the pool with `update-user-pool` and silently drops the trigger | Note it in the team docs. A missing trigger shows up as `groups` vanishing from tokens (Phase 5 check) |
| R5 | Group membership changes don't show until the next token refresh | Already handled: user-service returns `token_refresh_required`, and the frontend refreshes |
| R6 | Two people change the shared pool or API at once | Change window (T7), announced to the team |

## 8. Cost

The Lambda runs once per login or token refresh (about once an hour per active user), and stays well inside Lambda's free tier. Access-token customization is included in the Essentials tier the pool already uses. **About $0** extra; the alarm costs about $0.10/month.

## 9. Effort

| Phase | Who | Rough time |
|---|---|---|
| 0 | Team | One discussion |
| 1–3 | user-service owner | Half a day |
| 4–6 | user-service owner, in the change window | About 1 hour |
| 7 | Each service owner | About 30 min each |
| 8 | user-service owner | 30 min |
