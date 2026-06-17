import asyncio
import json
import os
import requests
import websockets
from collections import Counter, defaultdict
from dotenv import load_dotenv

load_dotenv()

ALCHEMY_WS_URL = os.getenv("ALCHEMY_WS_URL")
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

REVEAL_SIGNATURES = [
    "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb",  # setBaseURI
    "0xd111515d4b3b11aaf0f4fc2cc46090bd2e6e8bb80c9add76e3f8a4a7e682f77b",  # Reveal
    "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",  # BaseURIUpdated
]


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, skipping alert.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def fetch_all_metadata(contract_address, limit=100):
    api_key = ALCHEMY_API_KEY
    url = f"https://eth-mainnet.g.alchemy.com/nft/v2/{api_key}/getNFTsForCollection"
    assets = []
    params = {
        "contractAddress": contract_address,
        "withMetadata": "true",
        "limit": 100,
    }
    next_token = None

    while len(assets) < limit:
        if next_token:
            params["startToken"] = next_token
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            nfts = data.get("nfts", [])
            if not nfts:
                break
            for nft in nfts:
                metadata = nft.get("metadata") or {}
                token_id = nft.get("id", {}).get("tokenId", "")
                if token_id.startswith("0x"):
                    token_id = str(int(token_id, 16))
                traits = metadata.get("attributes") or metadata.get("traits") or []
                assets.append({
                    "token_id": token_id,
                    "name": metadata.get("name") or f"#{token_id}",
                    "traits": traits,
                    "permalink": f"https://opensea.io/assets/ethereum/{contract_address}/{token_id}"
                })
                if len(assets) >= limit:
                    break
            next_token = data.get("nextToken")
            if not next_token:
                break
        except Exception as e:
            print(f"Fetch error: {e}")
            break

    return assets


def compute_rarity(assets):
    trait_count = defaultdict(Counter)
    total = len(assets)

    for asset in assets:
        for trait in asset.get("traits", []):
            t = trait.get("trait_type")
            v = trait.get("value")
            if t and v is not None:
                trait_count[t][v] += 1

    rarity_map = {}
    for t, values in trait_count.items():
        rarity_map[t] = {v: count / total for v, count in values.items()}

    for asset in assets:
        score = 0.0
        for trait in asset.get("traits", []):
            t = trait.get("trait_type")
            v = trait.get("value")
            if t in rarity_map and v in rarity_map[t]:
                freq = rarity_map[t][v]
                if freq > 0:
                    score += 1.0 / freq
        asset["rarity_score"] = round(score, 2)

    return sorted(assets, key=lambda x: x["rarity_score"], reverse=True)


def handle_reveal(contract_address):
    print(f"\n🚨 REVEAL DETECTED: {contract_address}")
    print("Fetching metadata...")

    assets = fetch_all_metadata(contract_address, limit=500)
    if not assets:
        print("No metadata found yet.")
        return

    ranked = compute_rarity(assets)
    top20 = ranked[:20]

    print(f"\nTop 20 rarest tokens in {contract_address}:\n")
    for i, asset in enumerate(top20, 1):
        print(f"{i}. {asset['name']} — Score: {asset['rarity_score']} — {asset['permalink']}")

    # Send Telegram alert
    lines = [f"🚨 *REVEAL DETECTED*\nContract: `{contract_address}`\n\n*Top 10 Rarest:*"]
    for i, asset in enumerate(top20[:10], 1):
        lines.append(f"{i}. {asset['name']} | Score: {asset['rarity_score']} | [View]({asset['permalink']})")
    send_telegram("\n".join(lines))


async def watch_contract(contract_address):
    ws_url = f"wss://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    print(f"Watching contract: {contract_address}")

    subscribe_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_subscribe",
        "params": [
            "logs",
            {
                "address": contract_address,
                "topics": [REVEAL_SIGNATURES]
            }
        ]
    }

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps(subscribe_payload))
        print("Subscribed. Listening for reveal event...")

        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=60)
                data = json.loads(msg)
                if "params" in data:
                    print("Reveal event detected!")
                    handle_reveal(contract_address)
            except asyncio.TimeoutError:
                print("Still watching...")
            except Exception as e:
                print(f"WebSocket error: {e}")
                break


def start_watcher(contract_address):
    asyncio.run(watch_contract(contract_address))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python watcher.py <contract_address>")
        sys.exit(1)
    start_watcher(sys.argv[1])