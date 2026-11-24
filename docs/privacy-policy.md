# Privacy policy

> **Draft for legal review, not legal advice.** Written on 2026-10-30 from what the code
> and the design documents say the service does. Text in `[brackets]` must be filled in or
> confirmed before publishing. Each ⚠️ marks a promise the code does not keep yet; fix
> the code or change the text before this goes live. Keep this file in step with
> `docs/observability.md` § What is never logged and `docs/configuration.md`.

**Last updated:** [date of publication]

## Who we are

[Operator's legal name], [address], [contact email for privacy requests] ("we") operate
the [product name] GitHub App, which reviews pull requests with a large language model
(LLM) and posts the findings back to GitHub. We are the controller for the personal data
described below. [Name of the data protection officer or contact, if one is required.]

## What we process, and why

### When you sign in to the console

You sign in with your GitHub account. From GitHub we receive your **user id, login,
display name and avatar URL**, and the **organisations** you belong to, so we can show the
repositories you can manage.

- GitHub gives us an **access token** for your session. It is kept only in the server's
  memory, never in the database or the logs, and is gone when the session ends.
- Each successful sign-in is recorded in our logs with your **GitHub user id** and the
  number of your organisations (not their names), so that we can investigate misuse of an
  account.

### When the App reviews a pull request

When the App is installed on a repository and a pull request is opened or updated, we
receive the event from GitHub and read the pull request.

**Stored in our database:** the repository id and name, pull request number and title,
the author's GitHub login and id and their role in the repository, branch names, commit
ids, the review's status and timings, and each finding we posted (file path, line,
severity, category, and the comment text as posted).

**Processed, but never stored or logged:** the code changes (the diff), the pull request
description and the repository's review rules file (`.review/rules.md`). They are read to
perform the review and sent to our LLM providers (below). Secrets that match known
patterns (API keys, tokens) are masked in our logs and error reports. ⚠️ The diff itself
is not yet masked before it is sent to the LLM providers.

### To keep the service running

- **Logs:** JSON records of what the service did (ids, statuses, durations, counts). Our
  logging allowlist and tests keep code, diffs, LLM prompts and replies, pull request
  titles and descriptions out of every log line. Our web server's error log (written only
  when a request fails) contains the visitor's **IP address and browser user agent**.
  Query strings are removed from it before it leaves the server, so sign-in codes never
  reach our log provider. The server's own copy keeps the full request, and only its
  most recent 30 MB: older lines are deleted.
- **Error reports:** when something fails, an error report with the technical details
  (error type, code location) and the installation, repository and pull request numbers.
  No request bodies and no personal data fields are sent. [If the console reports errors
  from your browser: the same way, with personal data collection off; confirm in the
  frontend.]
- **Metrics:** counts and durations only (requests, reviews, queue length). They contain
  no personal data.
- **Alerts to our team** (Telegram) carry an alert's name and an error type, never your
  data.

## Legal bases

[Legal review to confirm.] Reviewing pull requests and running the console is
**performance of our contract** with the account that installed the App (GDPR Art.
6(1)(b)). Pull request authors who are not our customers are covered by our **legitimate
interest** in providing the review the repository owner asked for (Art. 6(1)(f)).
Sign-in records, logs and error reports are kept on our **legitimate interest** in
keeping the service secure and working (Art. 6(1)(f)).

## Who receives the data

| Recipient | Role | What | Where |
|---|---|---|---|
| GitHub, Inc. | The platform the App runs on | Comments, check runs and reviews we post | [United States; GitHub's own terms apply] |
| Contabo GmbH | Hosting (our server and database) | Everything we store | Germany ([confirm the data centre region]) |
| [Eurouter: legal name] | LLM routing | Diff, pull request title and description, review rules | EU; routed to EU-hosted model providers, currently [Scaleway, OVHcloud: confirm] |
| [TypeSafe: legal name] | Rule screening and verifying findings | Diff, review rules, the findings | [confirm location]. Turned off with `LLM_REFLECTION_ENABLED=false` |
| Grafana Labs | Logs, metrics and alerting | Logs and metrics (above) | EU region |
| Functional Software, Inc. (Sentry) | Error reports | Error reports (above) | EU region |
| Telegram | Alerts to our team | Alert names and error types | [confirm] |

[Confirm a data processing agreement with each processor, and whether each LLM provider
retains or trains on submitted content; state it here.] We do not sell personal data,
and we use it for no advertising.

## How long we keep it

| Data | Kept for |
|---|---|
| Console session and its access token | Until you sign out or the session expires ([1 hour by default: confirm the production setting]); a server restart also ends it |
| Review records and posted findings | [30 days] (`job_retention_days`), and removed when the App is uninstalled (`purge_on_uninstall`). ⚠️ The automatic purge is not built yet: until it is, records stay until deleted by hand |
| Logs on our server | 14 days; the web server's error log, its most recent 30 MB ([about N days at our traffic: measure on the server]) |
| Logs in Grafana Cloud | 14 days |
| Error reports in Sentry | [confirm the plan's retention] |
| Backups | [none yet; when introduced: N days] |

## Cookies

The console uses only cookies it needs to work: a **session cookie** (`__Host-session`),
for as long as you are signed in, and a short-lived **sign-in cookie** that protects the
GitHub sign-in against forgery, for 10 minutes. No analytics, advertising or third-party
cookies.

## Transfers outside the EU

We keep data in the EU where we choose the region. GitHub is based in the United States;
data the App posts to GitHub is governed by GitHub's terms. [Legal review: list any other
transfer and its safeguard, e.g. standard contractual clauses.]

## Your rights

You can ask us for access to your personal data, and to correct, delete, restrict or
transfer it, and you can object to processing based on legitimate interest. Write to
[contact email]. We answer within one month. You can also complain to a data protection
authority, for example [the authority for the operator's location].

Uninstalling the App stops all new processing for that repository or account.

## Security

All traffic uses TLS. Credentials are kept in our deployment's secret store, never in
the code or the logs. We store no code, diffs, prompts or LLM replies. Access to the
server, database and monitoring is limited to [the team members who operate the service].

## Changes

We will post changes here and update the date at the top. [For material changes: how
customers are notified.]
