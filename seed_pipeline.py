"""
Creates a second sample database: the marketing and sales pipeline of a fictional
B2B SaaS company (invoice automation for finance teams), shaped like the data a
B2B marketing team actually works with:

    channels, campaigns     where demand comes from (LinkedIn Ads, Google Ads, 6sense ABM ...)
    ad_spend                daily cost, impressions and clicks per paid campaign (ad platforms)
    ga4_sessions            daily website sessions and conversions per channel and device (GA4 export)
    accounts, contacts      companies and leads with their original source (CRM, HubSpot style)
    deals                   opportunities with stage, amount, owner and source channel (CRM)

The data is random but repeatable (fixed seed). It has the patterns such data has in
real life: LinkedIn and ABM bring fewer but larger enterprise deals, Meta brings cheap
leads that rarely qualify, organic search brings steady pipeline with no media spend,
and weekends are quiet.

Run:
    python seed_pipeline.py            # pipeline.db (SQLite)
    ASKDB_SAMPLE=pipeline python app.py
"""

import math
import random
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).with_name("pipeline.db")

SCHEMA = """
CREATE TABLE channels (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE,
    kind  TEXT NOT NULL CHECK (kind IN ('paid', 'organic'))    -- paid = has media spend
);

CREATE TABLE campaigns (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    channel_id  INTEGER NOT NULL REFERENCES channels(id),
    started_on  TEXT    NOT NULL,      -- ISO date
    ended_on    TEXT                   -- NULL while the campaign is running
);

CREATE TABLE ad_spend (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id),
    spend_date   TEXT    NOT NULL,     -- ISO date, one row per campaign and day
    impressions  INTEGER NOT NULL,
    clicks       INTEGER NOT NULL,
    cost_eur     REAL    NOT NULL
);

CREATE TABLE ga4_sessions (
    id                INTEGER PRIMARY KEY,
    channel_id        INTEGER NOT NULL REFERENCES channels(id),
    session_date      TEXT    NOT NULL,   -- ISO date, one row per channel, device and day
    device            TEXT    NOT NULL CHECK (device IN ('desktop', 'mobile', 'tablet')),
    sessions          INTEGER NOT NULL,
    engaged_sessions  INTEGER NOT NULL,
    conversions       INTEGER NOT NULL    -- demo requests and contact forms
);

CREATE TABLE accounts (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    industry   TEXT    NOT NULL,
    country    TEXT    NOT NULL,
    segment    TEXT    NOT NULL CHECK (segment IN ('SMB', 'Mid-Market', 'Enterprise')),
    employees  INTEGER NOT NULL
);

CREATE TABLE contacts (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    first_name   TEXT    NOT NULL,
    last_name    TEXT    NOT NULL,
    email        TEXT    NOT NULL UNIQUE,
    job_title    TEXT    NOT NULL,
    channel_id   INTEGER NOT NULL REFERENCES channels(id),   -- original source
    campaign_id  INTEGER REFERENCES campaigns(id),           -- NULL for channels without campaigns
    created_at   TEXT    NOT NULL,     -- ISO date and time the lead was created
    mql_at       TEXT                  -- ISO date it became marketing qualified; NULL = never
);

CREATE TABLE deals (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    contact_id   INTEGER NOT NULL REFERENCES contacts(id),
    channel_id   INTEGER NOT NULL REFERENCES channels(id),   -- original source of the contact
    campaign_id  INTEGER REFERENCES campaigns(id),
    owner        TEXT    NOT NULL,     -- account executive
    stage        TEXT    NOT NULL CHECK (stage IN ('discovery', 'demo', 'proposal', 'negotiation',
                                                   'closed_won', 'closed_lost')),
    amount_eur   REAL    NOT NULL,     -- annual contract value
    created_at   TEXT    NOT NULL,     -- ISO date the deal entered Stage 1 (discovery)
    closed_at    TEXT                  -- ISO date it was won or lost; NULL while open
);

CREATE INDEX idx_spend_campaign ON ad_spend(campaign_id);
CREATE INDEX idx_spend_date ON ad_spend(spend_date);
CREATE INDEX idx_sessions_date ON ga4_sessions(session_date);
CREATE INDEX idx_contacts_created ON contacts(created_at);
CREATE INDEX idx_deals_created ON deals(created_at);
CREATE INDEX idx_deals_channel ON deals(channel_id);
"""

TABLES = ["channels", "campaigns", "ad_spend", "ga4_sessions", "accounts", "contacts", "deals"]

START = date(2025, 5, 1)
END = date(2026, 10, 25)
SEGMENTS = ["SMB", "Mid-Market", "Enterprise"]

# name, kind, leads/day, lead->MQL, MQL->deal, win rate, segment mix (SMB, Mid-Market, Enterprise),
# website sessions/day, session->conversion rate, engaged share
CHANNELS = [
    ("LinkedIn Ads", "paid", 1.5, 0.34, 0.42, 0.24, (15, 45, 40), 180, 0.011, 0.52),
    ("Google Ads", "paid", 2.6, 0.26, 0.30, 0.20, (50, 38, 12), 420, 0.018, 0.58),
    ("Meta Ads", "paid", 2.0, 0.12, 0.18, 0.12, (70, 25, 5), 380, 0.006, 0.34),
    ("6sense ABM", "paid", 0.5, 0.48, 0.55, 0.30, (5, 35, 60), 60, 0.009, 0.47),
    ("Organic Search", "organic", 2.2, 0.24, 0.34, 0.25, (45, 40, 15), 900, 0.008, 0.61),
    ("Direct", "organic", 0.8, 0.30, 0.40, 0.28, (35, 40, 25), 350, 0.012, 0.66),
    ("Referral", "organic", 0.5, 0.45, 0.55, 0.38, (30, 45, 25), 90, 0.021, 0.70),
    ("Email", "organic", 0.9, 0.30, 0.30, 0.22, (40, 40, 20), 150, 0.016, 0.72),
    ("Events", "organic", 0.6, 0.40, 0.45, 0.27, (15, 45, 40), 40, 0.030, 0.75),
]

# name, channel, start, end, budget EUR/day, CPM EUR, click-through rate (budget 0 = no media spend)
CAMPAIGNS = [
    ("CFO Persona ABM", "LinkedIn Ads", date(2025, 5, 1), None, 160, 38.0, 0.006),
    ("Product Demo Retargeting", "LinkedIn Ads", date(2025, 5, 1), None, 90, 31.0, 0.009),
    ("Finance Leaders Webinar Promo", "LinkedIn Ads", date(2025, 9, 1), date(2025, 11, 30), 120, 35.0, 0.007),
    ("Enterprise Lookalike Audience", "LinkedIn Ads", date(2026, 6, 1), None, 140, 41.0, 0.005),
    ("Brand Search", "Google Ads", date(2025, 5, 1), None, 60, 48.0, 0.110),
    ("Competitor Search", "Google Ads", date(2025, 5, 1), None, 80, 55.0, 0.045),
    ("Non-Brand Invoicing Keywords", "Google Ads", date(2025, 7, 1), None, 150, 44.0, 0.038),
    ("Performance Max Trial", "Google Ads", date(2026, 2, 1), date(2026, 5, 31), 110, 16.0, 0.014),
    ("Lookalike Prospecting", "Meta Ads", date(2025, 5, 1), None, 70, 9.0, 0.011),
    ("Ebook Lead Gen", "Meta Ads", date(2025, 8, 1), None, 60, 8.0, 0.014),
    ("Retargeting Carousel", "Meta Ads", date(2025, 5, 1), date(2026, 3, 31), 40, 11.0, 0.016),
    ("In-Market Accounts Display", "6sense ABM", date(2025, 6, 1), None, 110, 14.0, 0.003),
    ("Tier 1 Account Surge", "6sense ABM", date(2026, 1, 1), None, 130, 17.0, 0.004),
    ("Monthly Newsletter", "Email", date(2025, 5, 1), None, 0, 0, 0),
    ("Trial Nurture Sequence", "Email", date(2025, 5, 1), None, 0, 0, 0),
    ("Annual Finance Summit Booth", "Events", date(2025, 5, 1), None, 0, 0, 0),
    ("Quarterly Product Webinar", "Events", date(2025, 5, 1), None, 0, 0, 0),
]

INDUSTRIES = [("Software", 22), ("Fintech", 16), ("E-commerce", 14), ("Logistics", 12), ("Manufacturing", 12),
              ("Healthcare", 10), ("Professional Services", 9), ("Education", 5)]
COUNTRIES = [("Germany", 24), ("United Kingdom", 20), ("Netherlands", 12), ("France", 11), ("United States", 10),
             ("Sweden", 7), ("Spain", 6), ("Poland", 5), ("Bulgaria", 3), ("Ireland", 2)]
NAME_A = ["Alder", "Brightline", "Cobalt", "Dunmore", "Elmfield", "Fernhill", "Greywater", "Harrow", "Ironbridge",
          "Juniper", "Kestrel", "Lakeshore", "Marlow", "Northgate", "Oakhurst", "Pinecrest", "Quarry", "Redwing",
          "Stonegate", "Thornbury", "Upland", "Valemont", "Westbrook", "Yarrow", "Zennor"]
NAME_B = ["Systems", "Labs", "Group", "Logistics", "Health", "Works", "Partners", "Digital", "Industries", "Trading",
          "Solutions", "Cloud", "Freight", "Medical", "Studio", "Holdings", "Networks", "Supply", "Analytics",
          "Robotics"]
EMPLOYEES = {"SMB": (10, 99), "Mid-Market": (100, 999), "Enterprise": (1000, 12000)}
ACCOUNTS_PER_SEGMENT = {"SMB": 250, "Mid-Market": 170, "Enterprise": 80}

FIRST = ["Lucas", "Emma", "Jonas", "Marek", "Ingrid", "Pedro", "Elena", "Tomas", "Clara", "Felix", "Nadia", "Oscar",
         "Petra", "Irene", "Viktor", "David", "Laura", "Sofia", "Anton", "Greta"]
LAST = ["Novak", "Schmidt", "Jansen", "Dubois", "Rossi", "Kowalski", "Lindqvist", "Murphy", "Garcia", "Petrov",
        "Becker", "Meyer", "Larsen", "Silva", "Weber"]
TITLES = [("CFO", 12), ("VP Finance", 12), ("Head of Finance", 16), ("Finance Manager", 22), ("Financial Controller", 14),
          ("Accounts Payable Lead", 12), ("Operations Manager", 8), ("Founder", 4)]

# Account executives by segment, and how each segment sells.
OWNERS = {"SMB": ["Marta Kowalska", "Liam Murphy"], "Mid-Market": ["Jonas Becker", "Sofia Rossi"],
          "Enterprise": ["Elena Petrova", "David Lindqvist"]}
DEAL_SIZE = {"SMB": (math.log(6000), 0.40), "Mid-Market": (math.log(22000), 0.45), "Enterprise": (math.log(70000), 0.50)}
CYCLE_DAYS = {"SMB": (20, 50), "Mid-Market": (35, 90), "Enterprise": (60, 150)}
WIN_FACTOR = {"SMB": 1.10, "Mid-Market": 1.00, "Enterprise": 0.85}
OPEN_STAGES = ["discovery", "demo", "proposal", "negotiation"]
DEVICES = [("desktop", 0.68), ("mobile", 0.28), ("tablet", 0.04)]


def _poisson(rng: random.Random, mean: float) -> int:
    """Knuth's method; the means here are small."""
    if mean <= 0:
        return 0
    if mean > 30:
        return max(0, round(rng.gauss(mean, math.sqrt(mean))))
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _weighted(rng: random.Random, pairs):
    return rng.choices([p[0] for p in pairs], [p[1] for p in pairs])[0]


def generate(seed: int = 2026) -> dict[str, list[tuple]]:
    """Rows for every table, the same for every run with the same seed."""
    rng = random.Random(seed)
    data: dict[str, list[tuple]] = {t: [] for t in TABLES}

    channel_id = {}
    for cid, row in enumerate(CHANNELS, start=1):
        channel_id[row[0]] = cid
        data["channels"].append((cid, row[0], row[1]))

    campaigns = []  # (id, channel_id, start, end, budget, cpm, ctr)
    for cid, (name, channel, start, end, budget, cpm, ctr) in enumerate(CAMPAIGNS, start=1):
        campaigns.append((cid, channel_id[channel], start, end, budget, cpm, ctr))
        data["campaigns"].append((cid, name, channel_id[channel], start.isoformat(), end.isoformat() if end else None))

    # Accounts: every name is a unique pair, shuffled so segments are mixed.
    names = [f"{a} {b}" for a in NAME_A for b in NAME_B]
    rng.shuffle(names)
    by_segment: dict[str, list[tuple]] = {s: [] for s in SEGMENTS}
    for segment in SEGMENTS:
        for _ in range(ACCOUNTS_PER_SEGMENT[segment]):
            aid = len(data["accounts"]) + 1
            account = (aid, names[aid - 1], _weighted(rng, INDUSTRIES), _weighted(rng, COUNTRIES), segment,
                       rng.randint(*EMPLOYEES[segment]))
            data["accounts"].append(account)
            by_segment[segment].append(account)

    spend_id = session_id = contact_id = deal_id = 0
    day = START
    while day <= END:
        weekday = 0.35 if day.weekday() >= 5 else 1.0            # B2B: weekends are quiet
        growth = 1 + (day - START).days / 1100
        season = 0.7 if day.month in (8, 12) else 1.15 if day.month in (3, 9, 10) else 1.0
        iso = day.isoformat()
        active = [c for c in campaigns if c[2] <= day and (c[3] is None or day <= c[3])]

        # Ad platforms: one row per paid campaign and day.
        for cid, _ch, _s, _e, budget, cpm, ctr in active:
            if not budget:
                continue
            spend_id += 1
            cost = round(budget * rng.uniform(0.7, 1.3) * (0.6 if weekday < 1 else 1.0) * growth, 2)
            impressions = int(cost / cpm * 1000)
            clicks = int(impressions * ctr * rng.uniform(0.8, 1.2))
            data["ad_spend"].append((spend_id, cid, iso, impressions, clicks, cost))

        for ch, (_name, _kind, leads, mql_rate, opp_rate, win_rate, mix, sessions, conv_rate, engaged) in enumerate(
                CHANNELS, start=1):
            # GA4: one row per channel, device and day.
            for device, share in DEVICES:
                session_id += 1
                n = int(sessions * share * weekday * growth * season * rng.uniform(0.8, 1.2))
                rate = conv_rate * (0.45 if device == "mobile" else 1.0)
                data["ga4_sessions"].append((session_id, ch, iso, device, n,
                                             int(n * min(0.95, engaged * rng.uniform(0.9, 1.1))),
                                             min(n, _poisson(rng, n * rate))))

            # CRM: new leads, some of which qualify and become deals.
            channel_campaigns = [c[0] for c in active if c[1] == ch]
            for _ in range(_poisson(rng, leads * weekday * growth * season)):
                contact_id += 1
                segment = rng.choices(SEGMENTS, mix)[0]
                account = rng.choice(by_segment[segment])
                first, last = rng.choice(FIRST), rng.choice(LAST)
                campaign = rng.choice(channel_campaigns) if channel_campaigns else None
                created = datetime.combine(day, datetime.min.time()) + timedelta(minutes=rng.randint(8 * 60, 19 * 60))
                mql_day = day + timedelta(days=rng.randint(0, 14)) if rng.random() < mql_rate else None
                if mql_day and mql_day > END:
                    mql_day = None
                data["contacts"].append((contact_id, account[0], first, last,
                                         f"{first}.{last}.{contact_id}@example.com".lower(), _weighted(rng, TITLES),
                                         ch, campaign, created.isoformat(sep=" ", timespec="minutes"),
                                         mql_day.isoformat() if mql_day else None))
                if not mql_day or rng.random() >= opp_rate:
                    continue
                opened = mql_day + timedelta(days=rng.randint(2, 21))
                if opened > END:
                    continue
                deal_id += 1
                mu, sigma = DEAL_SIZE[segment]
                amount = float(round(rng.lognormvariate(mu, sigma) / 100) * 100)
                cycle = rng.randint(*CYCLE_DAYS[segment])
                won = rng.random() < win_rate * WIN_FACTOR[segment]
                if not won:
                    cycle = max(5, int(cycle * rng.uniform(0.4, 1.0)))   # lost deals die earlier
                closed = opened + timedelta(days=cycle)
                if closed <= END:
                    stage, closed_at = ("closed_won" if won else "closed_lost"), closed.isoformat()
                else:
                    progress = (END - opened).days / cycle
                    stage, closed_at = OPEN_STAGES[min(3, int(progress * 4))], None
                data["deals"].append((deal_id, f"{account[1]} - New Business", account[0], contact_id, ch, campaign,
                                      rng.choice(OWNERS[segment]), stage, amount, opened.isoformat(), closed_at))
        day += timedelta(days=1)
    return data


def build(path: Path = DB_PATH, seed: int = 2026) -> None:
    data = generate(seed)
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA)
        for table, rows in data.items():
            db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in rows[0])})", rows)
        db.commit()
    print(f"Created {path.name}: " + ", ".join(f"{len(rows)} {t}" for t, rows in data.items()))


if __name__ == "__main__":
    build()
