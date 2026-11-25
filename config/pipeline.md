# Business rules: sample B2B pipeline (marketing and sales)

These rules are sent to the model with the database schema. The data combines three
sources a B2B marketing team reports on: ad platforms (`ad_spend`), GA4 (`ga4_sessions`)
and the CRM (`accounts`, `contacts`, `deals`).

- **Pipeline** (pipeline created) is the sum of `deals.amount_eur` for deals whose
  `created_at` falls in the period, whatever happened to them later. A deal is created
  when it enters Stage 1 (`discovery`), so "Stage 1 opportunities" means deals.
- **Open pipeline** is the amount of deals whose stage is not `closed_won` or `closed_lost`.
- **Won revenue** is the sum of `amount_eur` for `closed_won` deals, dated by `closed_at`
  (not `created_at`). `amount_eur` is annual contract value.
- **Win rate** is won deals divided by closed deals (`closed_won` + `closed_lost`), dated
  by `closed_at`. Open deals are not in the denominator.
- **Sales cycle** is the days from `created_at` to `closed_at` of won deals.
- A **lead** is a row in `contacts`, dated by `created_at`. An **MQL** is a contact with
  `mql_at` set, dated by `mql_at`. **MQL rate** is MQLs divided by leads created in the period.
- **Attribution is original source**: `contacts.channel_id` and `deals.channel_id` hold the
  channel that first brought the contact. Use `deals.channel_id` for pipeline by channel.
  `campaign_id` is NULL for channels that run no campaigns (Organic Search, Direct, Referral).
- `channels.kind` is `paid` for channels with media spend (LinkedIn Ads, Google Ads,
  Meta Ads, 6sense ABM) and `organic` for the rest, including Email and Events.
- **Never join two fact tables row by row.** `ad_spend`, `ga4_sessions`, `contacts` and
  `deals` have different grains, so a direct join multiplies rows. For cost per lead,
  cost per opportunity, ROAS or pipeline per euro, aggregate each table to the channel
  (or campaign) in its own CTE first, then join the CTEs.
- **Cost per opportunity** is ad spend divided by deals created; **ROAS** here is won
  revenue divided by ad spend. Both only make sense for paid channels.
- Session **conversion rate** is `conversions / sessions`; **engagement rate** is
  `engaged_sessions / sessions`. GA4 conversions are form fills, not CRM leads, so the
  two counts do not match exactly.
- Quarters are calendar quarters. All money is in EUR.
