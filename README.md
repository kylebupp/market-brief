# market-brief

Daily pre-market briefing to Telegram, ~7:30 AM ET on weekdays, via GitHub
Actions. Runs whether or not any personal machine is awake.

## Timing — read this before trusting the schedule

GitHub does not deliver scheduled events on time. Measured over
months on another repo: **2-9 hours late, median ~3.5h**. A single `30 11 * * 1-5`
cron would arrive after the open most days, which for a pre-market brief is
worthless.

The workaround: fire every 30 minutes across 02:00-13:30 UTC, then gate on
arrival. A run sends only if, at the moment it actually executes, all of
these hold:

- it is a weekday in America/New_York
- the ET clock reads between 07:15 and 08:45
- `state/last_sent.txt` does not already contain today's ET date

Everything else exits in a few seconds having done nothing. The gate reads
the *real* clock, so DST needs no special handling and no UTC offset is
hardcoded anywhere.

**This is a mitigation, not a fix.** If every delivery on a given morning
lands outside the window, no brief goes out that day. Cloud Run with Cloud
Scheduler fires on time and is the better host if the account setup is ever
worth doing.

## Once-per-day marker

`state/last_sent.txt` holds the last ET date sent, committed back by the
workflow. That is what stops the second and third deliveries of the morning
from sending duplicates. The `concurrency` group serialises runs so two
simultaneous arrivals cannot both pass the check.

A manual `workflow_dispatch` bypasses the window (for testing) and
deliberately does **not** write the marker, so a test cannot suppress that
morning's real brief.

## Secrets

Repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

To re-read the token: Telegram → @BotFather → `/mybots` → the bot → **API
Token**. Avoid `/revoke` — it rotates the token and breaks anything else
using the same bot until those secrets are updated too.

## Testing

Actions → Pre-market brief → **Run workflow**. Bypasses the window, sends
immediately, leaves the marker untouched.

## Data sources

No API keys. Yahoo v8 chart (futures, sectors, movers), CNBC (10Y yield —
Yahoo's `^TNX` freezes at 15:00 ET and would serve stale values pre-market),
Nasdaq (earnings, most-actives), ForexFactory + Nasdaq merged (economic
calendar).

Every section degrades independently to a `⚠️ [section] unavailable` line.
The run exits non-zero only if every section fails or Telegram rejects the
message, which surfaces as a red X in the Actions tab.

**Untested risk:** these are unofficial free endpoints. They may throttle or
block GitHub's datacenter egress in ways they would not block a home
connection. The first real morning run is the test; check the Actions log for
`FAIL HTTPError`.

## Local copy

A parallel setup exists on a local machine with an OS-level scheduled task and
a fuller README covering data-source gotchas, the economic-calendar condensing
rules, and the mover diagnostics. Keep only one of the two enabled, or two
briefs arrive each morning.
