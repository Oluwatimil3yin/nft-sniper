# NFT Rarity Sniper

**Front-run NFT reveals & mints** — get accurate rarity scores **before OpenSea** updates.

## Core Superpower: Reveal Front-Running

When a collection reveals (hidden → real traits), this tool:

- Detects the reveal tx/event via WebSocket
- **Auto-extracts the new baseURI** from the transaction calldata when possible
- Fires **parallel Alchemy + direct raw metadata fetches** (fastest path)
- Builds accurate rarity from the live revealed traits on the fly
- Ranks + Telegram alerts the top ones **before OpenSea** sees them

This is often 30 seconds to several minutes ahead of OpenSea indexing.

## Why this matters

Most rarity tools compute scores from a tiny sample of the collection.  
This sniper lets you:

1. Build a **full-collection trait frequency map** once (accurate).
2. Watch the blockchain in real time for **new mints**.
3. **Front-run reveals**: scrape + compute rarity the moment metadata goes live on-chain.
4. Get Telegram alerts on high-rarity items **before OpenSea shows them**.

## Setup

```powershell
cd nft-sniper
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env
# edit .env — ALCHEMY_API_KEY is mandatory for speed
```

### Required Keys

- `ALCHEMY_API_KEY` (strongly recommended)
- `OPENSEA_API_KEY` (optional, for floor + listings)
- Telegram bot for alerts (optional but powerful for sniping)

Get Alchemy key: https://dashboard.alchemy.com/

## Core Workflow (Recommended)

### 1. (Optional) Pre-build a rarity map for better scoring on mints

```powershell
python build_snapshot.py 0xYourContract 10000
```

Saves to `rarity_maps/`.

For brand-new reveals you usually want a fresh scrape anyway (see below).

### 2. Reveal front-runner (main feature)

```powershell
# Live listener (waits for the reveal tx to land)
python watcher.py 0xYourContract --mode reveal --supply 10000 --threshold 180

# IMMEDIATE BLAST (manual trigger)
python watcher.py 0xYourContract --blast --supply 10000

# With a known base URI for direct fastest scraping
python watcher.py 0xYourContract --blast --base-uri "ipfs://Qm.../" --supply 10000

# Blast + keep watching for more events
python watcher.py 0xYourContract --blast --watch --mode reveal --supply 10000
```

What happens on reveal / blast:
- Detects reveal event or you force it.
- **Auto-extracts baseURI** from the triggering transaction calldata when possible.
- Runs **parallel Alchemy refresh scrape** + **direct raw metadata scrape** (if baseURI found).
- Builds accurate on-the-fly rarity map from the live data.
- Ranks and fires Telegram alert with top rare tokens **before OpenSea**.

This combination is your best chance to see real rarity scores first.

Other useful flags:

```powershell
python watcher.py 0xCONTRACT --mode both --supply 10000 --threshold 200 --workers 80
python watcher.py 0xCONTRACT --mode reveal --supply 5000
```

### 3. Mint sniper (ongoing drops)

```bash
python watcher.py 0xContract --mode mint --threshold 160
```

(Use after you ran the snapshot builder for accuracy.)

### 4. One-off scans still work

See `sniper.py` and the web UI (`app.py`). They also benefit from prebuilt maps now.

- Listens for mints (Transfer from 0x0) via Alchemy WebSocket.
- Fetches fresh metadata immediately.
- Scores using your prebuilt map.
- Sends Telegram alert for anything above `--threshold`.

Flags:
- `--threshold 180` (default 150)
- `--map path/to/custom.json`

### 3. One-off scans (web or CLI)

**Web UI**
```bash
python app.py
# open http://localhost:5000
```

**CLI**
```bash
python sniper.py --contract 0x... --limit 200 --top 15
python sniper.py --collection boredapeyachtclub --top 20
```

## Files

- `build_snapshot.py` — Build accurate rarity map
- `watcher.py` — Real-time mint sniper + Telegram alerts
- `sniper.py` — Fast CLI scanner
- `app.py` + `templates/` — Simple web UI
- `rarity_utils.py` — Shared accurate rarity + Alchemy helpers

## Tips for beating OpenSea

- Always use a prebuilt rarity map (`build_snapshot.py`)
- Use Alchemy (fastest metadata + WS)
- Set high `--threshold` for only the real gems
- Run the watcher 24/7 on a cheap VPS or your machine
- For IPFS-heavy collections, metadata can appear 1-10s after the mint tx

## Notes

- Currently Ethereum mainnet focused (easy to extend)
- OpenSea v1 APIs are old; the sniper prefers on-chain + Alchemy for freshness
- Always respect rate limits

Happy sniping.
