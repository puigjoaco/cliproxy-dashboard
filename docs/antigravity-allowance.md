# Antigravity allowance and account details

Antigravity account cards read Gemini quota observations through the gateway's
selected OAuth credential, using `fetchAvailableModels`. The request uses that
credential's project ID; the project ID and tokens are not sent to the browser.
Only reported Gemini model quotas are shown. Missing models or missing quota
data do not imply an unused allowance.

The UI collapses matching Gemini observations into **Gemini · shared quota**
only when their percentage, known reset time and measurement timestamp match.
Different observations remain visible. Other providers and Claude's independent
Fable window retain their own presentation. This grouping follows Google's
[shared Gemini quota description](https://antigravity.google/blog/changes-to-antigravity-plans/);
it does not sum model allowances or invent a reset interval.

A separate `loadCodeAssist` read supplies the reported plan and an optional,
explicit Google One AI credit balance. A paid tier takes precedence over the
base tier. Missing, invalid or ambiguous credit balances stay unknown. Credits
are presented separately from the Gemini quota percentage; observing a balance
does not authorize spending it.

Both reads use the existing refresh budget, cache and backoff mechanisms.
Failed refreshes preserve the last observation and label it as last known.
Credential identity must match before an observation can populate an account
card. Provider responses are reduced to allowlisted fields.

## Account-card details

- Claude shows the provider-reported 5× or 20× tier when available, and a
  confirmed **Extra usage: Off** state. Missing data is not treated as off or
  as a zero balance.
- Claude and Antigravity retain **Renews —** when no date is available. The
  account menu can store a confirmed billing date. Allowance resets never
  supply billing renewal dates.
- Speed and routing controls have explicit headings and values. A missing
  speed plugin means **Speed setting not applied**; an unknown enforcement
  state means **Speed setting not verified**. The explanation remains in the
  tooltip, and saved choices are preserved. The
  [full installer](../README.md#update-an-existing-installation) already installs
  the native plugin; this change does not replace its policy.

The quota, profile and UI tests use synthetic accounts and mocked transports.
They cover invalid/missing values, credential matching, exhausted protobuf
quotas, stale observations, backoff and independent windows without consuming
real account allowance.
