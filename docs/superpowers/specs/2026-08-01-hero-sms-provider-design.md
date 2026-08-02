# HeroSMS / SMS-Activate Provider Design

## Context

HeroSMS exposes an SMS-Activate-compatible endpoint at
`https://hero-sms.com/stubs/handler_api.php`. Its number lifecycle uses the
same request actions and text responses already handled by the project's
GrizzlySMS branch:

- `getNumber` -> `ACCESS_NUMBER:<activation_id>:<phone>`
- `getStatus` -> `STATUS_WAIT_CODE` or `STATUS_OK:<code>`
- `setStatus` with status `1`, `3`, `6`, or `8`

The existing transport is therefore reusable. The missing part is a clear,
configurable SMS-Activate/HeroSMS provider surface.

## Considered approaches

### 1. Replace the hard-coded Grizzly endpoint

This is the smallest code change, but it couples one checkout to one vendor and
makes switching providers require source edits. It also keeps misleading
Grizzly-only labels in the WebUI.

### 2. Add a separate HeroSMS client

This gives the vendor a dedicated module, but duplicates the same
`getNumber/getStatus/setStatus` protocol and error parsing already present in
`core/sms_provider.py`.

### 3. Generalize the existing compatible-handler branch (selected)

Keep `grizzly` working, add `sms_activate` as the canonical new provider and
accept `hero_sms` as an alias. Expose the handler URL through configuration and
reuse one request/parser implementation for all compatible providers.

## Configuration

`config/codex.py` will expose:

- `SMS_PROVIDER`: supports `grizzly`, `sms_activate`, `hero_sms`, `l`, and `h`.
- `SMS_API_BASE`: environment-overridable handler URL. Existing Grizzly URL
  remains the default to preserve current installations.
- `SMS_API_KEY`: shared API key for handler-compatible providers.
- `SMS_CANCEL_DELAY`: `-1` means provider default; Grizzly and
  SMS-Activate/HeroSMS use 125 seconds (120-second platform restriction plus a
  five-second buffer).
- Existing `SMS_SERVICE`, `SMS_COUNTRY`, and `SMS_MAX_PRICE` remain the request
  parameters. HeroSMS/OpenAI users should enter the service code exposed by
  their account/API documentation, commonly `dr` on SMS-Activate-compatible
  catalogs.

The WebUI will expose the API base and cancel delay alongside the existing SMS
fields. `.env.example` and `README.md` will document the new provider values.

## Runtime behavior

`core/sms_provider.py` will normalize provider spellings and route all
handler-compatible providers through one request function. Existing L and H
JSON backends remain unchanged.

The request function will continue to recognize existing error strings such as
`BAD_KEY`, `NO_BALANCE`, `NO_NUMBERS`, `BAD_ACTION`, and `NO_ACTIVATION`.
Successful number acquisition, OTP polling, completion, and cancellation keep
the public functions used by the Codex OAuth drivers unchanged.

Cancellation delay becomes provider-aware. Grizzly and HeroSMS/SMS-Activate
wait for the two-minute platform window plus a five-second buffer. Compatible
providers cancel synchronously so the next number is not acquired before the
previous cancellation request completes.

## Testing

Add focused tests that prove:

1. `sms_activate`, `sms-activate`, and `hero_sms` normalize to the compatible
   handler path.
2. Number acquisition sends the configured base URL, API key, service, country,
   and maximum price.
3. OTP polling parses `STATUS_OK:<code>`.
4. Completion sends status `6` and cancellation sends status `8` without the
   Grizzly delay.
5. WebUI and environment-backed configuration expose `SMS_API_BASE` and
   `SMS_CANCEL_DELAY`.
6. The existing full test suite remains green.

## Scope

This change adds the provider integration and configuration surface only. It
does not add balance purchasing, service/catalog browsing, rental-number APIs,
or automatic API-key extraction from the browser.
