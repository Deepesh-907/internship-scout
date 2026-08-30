# internship-scout (24/7 on GitHub Actions)

Scans job boards + company career pages every 30 minutes, scores each posting
against `profile.json`, and delivers alerts per your rules:

| Score | What happens |
|-------|--------------|
| 85-100 | Individual email/Telegram immediately — `🔥 [NN%] Match — Role at Company` |
| 70-84 | Collected into the 🟢 digest |
| 60-69 | 🟡 Stretch Match in the digest, with missing skills listed |
| < 60 | Silent, except a major company whose gaps are only preferred tech (💎, explained) |

Full-time rule: postings requiring joining before **15 Dec 2026** are skipped;
future cohorts / graduate programs (2027+) are reported and can fire immediate alerts.

## Where it runs

- **GitHub Actions** (primary): runs every 30 min even when your PC is off.
  State (`state/seen.json`) is committed back after each run so nothing is repeated.
- Locally (optional): `python scout.py --no-send` for a dry run, `python scout.py`
  to scan+deliver using `email.json`.

## One-time setup (GitHub)

1. Create a **private** GitHub repo and push this folder (or it's already pushed).
2. Repo → Settings → Secrets and variables → Actions → New repository secret,
   add any of (both channels optional, email is tried first):
   - `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`
     (Gmail app password: myaccount.google.com/apppasswords, needs 2-Step Verification on)
   - `TELEGRAM_BOT_TOKEN` (from @BotFather), `TELEGRAM_CHAT_ID` (message the bot once,
     then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`)
   - `DELIVERY` = `auto` (default) / `email` / `telegram`
3. Actions tab → enable workflows if prompted. Done — alerts arrive by themselves.
   Note: scheduled runs may be delayed a few minutes by GitHub; that's normal.

## Tuning

- `profile.json` — your skills, roles, target companies. Edit + commit; next run uses it.
- `email.json` — local credentials (never committed).
- Sources live in `scout.py` (`SOURCES`, `_gh_companies`, `_lever_companies`, …);
  dead sources auto-disable after 8 consecutive failures (see `state/source_errors.json`).
