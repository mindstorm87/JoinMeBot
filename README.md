# JoinMe Telegram Validation Bot

A minimal MVP bot to validate the JoinMe / Let's Have A Coffee idea in Telegram.

## What it does

- Registers user interest
- Lets user become `Open To Talk`
- Requests Telegram location
- Matches nearby users by distance and shared interests
- Lets another user send a `Wave`
- Lets the first user accept or decline
- Creates a private intro by sharing Telegram usernames
- Asks whether the meeting happened
- Stores validation metrics in SQLite

## Setup

1. Create a bot with Telegram @BotFather and copy the token.
2. Install Python 3.11+.
3. In this folder, run:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
```

4. Edit `.env` and paste your `BOT_TOKEN`.
5. Run:

```bash
python bot.py
```

## Commands

- `/start` — onboarding
- `/open` — become Open To Talk
- `/nearby` — see nearby people
- `/stats` — show validation metrics
- `/cancel` — cancel current Open To Talk status

## Important privacy note

This MVP does not expose exact coordinates to other users. It only shows approximate distance and interests. When a wave is accepted, it shares Telegram usernames if available.

For a real pilot, use public meeting places only, add moderation, and do not market it as dating.
