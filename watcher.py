"""
Real-time NFT Rarity Sniper (pre-OpenSea focused)

Monitors a contract for NEW MINTS via Alchemy WebSocket (Transfer from zero address).
As soon as a token is minted and metadata is available:
- Fetches metadata instantly via Alchemy
- Scores it using a pre-built accurate rarity map (from build_snapshot.py)
- Alerts on high-rarity mints via Telegram

This beats OpenSea indexing latency.

Usage:
    python watcher.py 0xYourContract [--threshold 180] [--map rarity_maps/0x....json]
"""
import asyncio
import json
import os
import sys
import time
from typing import Optional, Dict

import requests
import websockets
from dotenv import load_dotenv

from rarity_utils import (
    AlchemyNFTClient,
    build_trait_rarity_map,
    compute_rarity_score,
    load_rarity_map,
    scrape_reveal_and_compute,
    get_top_rare_from_reveal,
    extract_base_uri_from_calldata,
)

load_dotenv()

ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Standard ERC-721 Transfer event signature
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000000000000000000000000000"

# Common reveal / metadata update event topics (add more as you discover projects)
REVEAL_TOPICS = {
    # Reveal()
    "0x0f6798a560793a54c3bcfe86a93cde1e73087d944c0ea20544137d4121396885",
    # SetBaseURI(string)
    "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb",
    # BaseURIUpdated or similar
    "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",
    # MetadataUpdate (ERC-4906)
    "0xf8e1a15aba9398e019f0b49df1a4fde98ee17ae345cb5f6b5e2c27f5033e8ce7",
    # Revealed (some projects)
    "0x6e7b2b0a4f3f8e2c5a9b3e1d7c8f0a2b5e6d7c8f9a0b1c2d3e4f5a6b7c8d9e0f",
}

# Some projects just change state - we'll also trigger on any contract activity during reveal windows
REVEAL_TRIGGER_KEYWORDS = ["reveal", "baseuri", "metadata", "seturi"]


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Not configured — skipping alert.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=12)
        if r.status_code != 200:
            print(f"[Telegram] Failed: {r.text}")
    except Exception as e:
        print(f"[Telegram] Error: {e}")


def get_alchemy_client() -> Optional[AlchemyNFTClient]:
    if not ALCHEMY_API_KEY:
        print("ERROR: ALCHEMY_API_KEY is required")
        return None
    return AlchemyNFTClient(ALCHEMY_API_KEY)


def decode_mint_log(log: Dict) -> Optional[str]:
    """
    Decode ERC721 Transfer(from=0, to=..., tokenId) log.
    tokenId is usually topics[3] (padded uint256).
    """
    topics = log.get("topics", [])
    if len(topics) < 4:
        return None
    if topics[0].lower() != TRANSFER_TOPIC:
        return None
    if topics[1].lower() != ZERO_ADDRESS:
        return None  # not a mint
    # tokenId in topics[3]
    try:
        token_id = str(int(topics[3], 16))
        return token_id
    except Exception:
        return None


def build_permalink(contract: str, token_id: str) -> str:
    return f"https://opensea.io/assets/ethereum/{contract}/{token_id}"


class RaritySniper:
    def __init__(
        self,
        contract: str,
        rarity_map: Optional[Dict] = None,
        threshold: float = 150.0,
        supply: int = 10000,
        max_workers: int = 70,
        manual_base_uri: Optional[str] = None,
    ):
        self.contract = contract.lower()
        self.rarity_map = rarity_map or {}
        self.threshold = threshold
        self.supply = supply
        self.max_workers = max_workers
        self.manual_base_uri = manual_base_uri
        self.client = get_alchemy_client()
        self.seen = set()  # avoid duplicate alerts
        self.reveal_triggered = False
        self.last_reveal_time = 0

    def load_map(self, map_path: Optional[str] = None):
        if map_path and os.path.exists(map_path):
            try:
                with open(map_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.rarity_map = data.get("rarity_map", {})
                print(f"[Rarity] Loaded map from {map_path} (supply={data.get('total_supply')})")
                return
            except Exception as e:
                print(f"[Rarity] Failed to load map: {e}")

        # Try default location
        loaded = load_rarity_map(self.contract)
        if loaded:
            self.rarity_map = loaded.get("rarity_map", {})
            print(f"[Rarity] Loaded map for {self.contract} (supply ~{loaded.get('total_supply')})")
        else:
            print("[Rarity] WARNING: No prebuilt rarity map found.")
            print("         Run: python build_snapshot.py <contract> 10000")
            print("         Rarity scores will be inaccurate until a map is available.")

    def score_token(self, metadata: Dict) -> float:
        if not self.rarity_map:
            # Fallback: use the single token itself (useless but won't crash)
            return compute_rarity_score(metadata, build_trait_rarity_map([metadata]))
        return compute_rarity_score(metadata, self.rarity_map)

    def process_mint(self, token_id: str):
        if token_id in self.seen:
            return
        self.seen.add(token_id)

        print(f"\n🆕 NEW MINT detected: Token #{token_id}")

        if not self.client:
            print("No Alchemy client — cannot fetch metadata.")
            return

        # Fast metadata fetch (this is what allows beating OpenSea)
        meta = self.client.fetch_single_metadata(self.contract, token_id, refresh=True)
        if not meta or not meta.get("traits"):
            print("  Metadata not ready yet (common right after mint). Will retry once...")
            time.sleep(1.5)
            meta = self.client.fetch_single_metadata(self.contract, token_id, refresh=True)

        if not meta:
            print("  Could not fetch metadata.")
            return

        score = self.score_token(meta)
        name = meta.get("name", f"#{token_id}")
        link = build_permalink(self.contract, token_id)

        print(f"  {name} | Rarity Score: {score} | {link}")

        if score >= self.threshold:
            msg = (
                f"🔥 *HIGH RARITY MINT*\n"
                f"Contract: `{self.contract}`\n"
                f"Token: *{name}* (`#{token_id}`)\n"
                f"Rarity Score: **{score}**\n"
                f"[View on OpenSea]({link})"
            )
            send_telegram(msg)
            print("  🚨 Alert sent to Telegram!")
        else:
            print(f"  Below threshold ({self.threshold}). No alert.")

    def is_reveal_log(self, log: Dict) -> bool:
        """Heuristic: does this log look like a reveal / metadata update?"""
        topics = [t.lower() for t in log.get("topics", [])]
        for t in topics:
            if t in REVEAL_TOPICS:
                return True
        # Also check transaction input if available in some Alchemy payloads (rare)
        # Fallback: any activity on the contract can be treated as potential reveal trigger
        return False

    def handle_reveal_front_run(self, tx_hash: Optional[str] = None, force: bool = False):
        """The core 'front run at reveal' logic. Can be triggered by event or manually (--blast)."""
        now = time.time()
        if not force and self.reveal_triggered and (now - self.last_reveal_time) < 180:
            return

        self.reveal_triggered = True
        self.last_reveal_time = now

        print("\n" + "=" * 65)
        print("🚨🚨🚨  REVEAL / BLAST TRIGGERED — SCRAPING METADATA NOW  🚨🚨🚨")
        print(f"Contract: {self.contract}")
        print("Scraping in parallel to get rarity BEFORE OpenSea updates...")
        print("=" * 65)

        if not self.client:
            print("No Alchemy client available. Cannot scrape.")
            self.reveal_triggered = False
            return

        base_uri = self.manual_base_uri
        if not base_uri and tx_hash:
            print(f"[TX] Inspecting tx {tx_hash} for baseURI...")
            tx = self.client.fetch_transaction(tx_hash)
            if tx and tx.get("input"):
                base_uri = extract_base_uri_from_calldata(tx["input"])
                if base_uri:
                    print(f"[TX] Extracted baseURI candidate: {base_uri}")
                else:
                    print("[TX] No obvious baseURI string found in calldata.")

        # Do the heavy lift: Alchemy (always) + direct if we have base_uri
        ranked = scrape_reveal_and_compute(
            self.client,
            self.contract,
            supply=self.supply,
            start_token=0,
            max_workers=self.max_workers,
            build_and_save_map=True,
            base_uri=base_uri,
        )

        if not ranked:
            print("No tokens with traits scraped yet. Reveal might still be propagating.")
            self.reveal_triggered = False
            return

        top = get_top_rare_from_reveal(ranked, top_n=25)

        print(f"\n=== TOP {len(top)} RAREST (scraped fresh at reveal) ===")
        for i, token in enumerate(top, 1):
            name = token.get("name", f"#{token.get('token_id')}")
            score = token.get("rarity_score", 0)
            link = build_permalink(self.contract, token.get("token_id", ""))
            print(f"{i:2}. {name:<32} Score: {score:8.2f}  {link}")

        # Telegram alert
        lines = [
            f"🔥 *REVEAL FRONT-RUN ALERT* 🔥",
            f"`{self.contract}`",
            f"Tokens scraped: {len(ranked)}",
            "",
            "*TOP RARITY RIGHT NOW (pre-OpenSea):*",
        ]
        for i, t in enumerate(top[:12], 1):
            name = t.get("name", f"#{t.get('token_id')}")
            score = t.get("rarity_score", 0)
            link = build_permalink(self.contract, t.get("token_id", ""))
            lines.append(f"{i}. `{name}` — **{score}**  [OpenSea]({link})")

        lines.append("\n_Scraped directly from on-chain metadata._")

        send_telegram("\n".join(lines))
        print("\n✅ Top rarity list pushed to Telegram.")

        if ranked:
            self.rarity_map = build_trait_rarity_map(ranked)

        return ranked  # so blast mode can use it

    def process_log(self, log: Dict):
        """Central dispatcher for any log we receive from the contract."""
        token_id = decode_mint_log(log)
        if token_id:
            self.process_mint(token_id)
            return

        tx_hash = log.get("transactionHash") or (log.get("transaction") or {}).get("hash")

        if self.is_reveal_log(log):
            print(f"[LOG] Potential reveal-related event received (tx={tx_hash})")
            self.handle_reveal_front_run(tx_hash=tx_hash)
            return

        # For broad subscriptions, any new log on the contract during reveal-hunting period can be a trigger
        # We are conservative here — only trigger if user is in reveal mode or used --blast
        # (The main power comes from specific reveal topics + tx inspection)

    async def watch_contract(self, mode: str = "both"):
        """
        Unified real-time watcher.

        mode:
          - "mint"   : only new mints
          - "reveal" : prioritize reveal front-running (broader logs)
          - "both"   : catch mints + reveal events (recommended)
        """
        if not ALCHEMY_API_KEY:
            print("ALCHEMY_API_KEY missing")
            return

        ws_url = f"wss://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

        print(f"🚀 Starting unified watcher on {self.contract}")
        print(f"   Mode: {mode} | Threshold: {self.threshold} | Supply hint: {self.supply}")

        if mode == "mint":
            filter_params = {
                "address": self.contract,
                "topics": [TRANSFER_TOPIC, ZERO_ADDRESS],
            }
            print("   Subscribed narrowly to mints (Transfer from 0x0)")
        else:
            filter_params = {"address": self.contract}
            print("   Subscribed BROADLY to all contract logs (best for catching reveals)")

        subscribe = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": ["logs", filter_params],
        }

        while True:
            try:
                async with websockets.connect(ws_url) as ws:
                    await ws.send(json.dumps(subscribe))
                    print("✅ Connected. Listening... (Ctrl+C to stop)")

                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=80)
                            msg = json.loads(raw)

                            if msg.get("method") == "eth_subscription":
                                params = msg.get("params", {})
                                result = params.get("result", {})
                                self.process_log(result)

                        except asyncio.TimeoutError:
                            await ws.ping()
                        except websockets.ConnectionClosed:
                            print("WS closed, reconnecting...")
                            break
                        except Exception as e:
                            print(f"WS error: {e}")
                            await asyncio.sleep(1.5)

            except Exception as e:
                print(f"WS connect error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    def blast_now(self):
        """One-shot immediate full scrape (for --blast). No waiting for events."""
        print("\n[ BLAST ] Forcing immediate reveal-style scrape right now...")
        base = self.manual_base_uri
        if base:
            print(f"[BLAST] Using provided --base-uri: {base}")
        result = self.handle_reveal_front_run(force=True)
        # If manual base, re-run scrape logic with it if the handler didn't capture tx
        if base and self.client:
            print("[BLAST] Re-running scrape with provided base URI for direct path...")
            ranked = scrape_reveal_and_compute(
                self.client, self.contract, supply=self.supply,
                max_workers=self.max_workers, build_and_save_map=True, base_uri=base
            )
            # Re-send top if we got results
            if ranked:
                top = get_top_rare_from_reveal(ranked, 10)
                lines = [f"🔥 *BLAST with direct baseURI* 🔥", f"`{self.contract}`"] + \
                        [f"{i}. {t.get('name')} — **{t.get('rarity_score')}**" for i, t in enumerate(top,1)]
                send_telegram("\n".join(lines))
        return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python watcher.py <contract> [options]")
        print("\nReveal front-run examples:")
        print("  python watcher.py 0x... --mode reveal --supply 10000 --threshold 180")
        print("  python watcher.py 0x... --blast --supply 10000                    # immediate scrape now")
        print("  python watcher.py 0x... --blast --base-uri https://.../ --supply 10000")
        print("  python watcher.py 0x... --blast --watch --mode reveal")
        sys.exit(1)

    contract = sys.argv[1].lower()
    mode = "both"
    threshold = 180.0
    supply = 10000
    map_path = None
    max_workers = 70
    blast = False
    manual_base_uri = None

    i = 2
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--mode" and i + 1 < len(sys.argv):
            mode = sys.argv[i + 1].lower()
            i += 2
        elif arg == "--threshold" and i + 1 < len(sys.argv):
            threshold = float(sys.argv[i + 1])
            i += 2
        elif arg == "--supply" and i + 1 < len(sys.argv):
            supply = int(sys.argv[i + 1])
            i += 2
        elif arg == "--map" and i + 1 < len(sys.argv):
            map_path = sys.argv[i + 1]
            i += 2
        elif arg == "--workers" and i + 1 < len(sys.argv):
            max_workers = int(sys.argv[i + 1])
            i += 2
        elif arg == "--blast":
            blast = True
            i += 1
        elif arg == "--base-uri" and i + 1 < len(sys.argv):
            manual_base_uri = sys.argv[i + 1]
            i += 2
        else:
            i += 1

    sniper = RaritySniper(
        contract,
        threshold=threshold,
        supply=supply,
        max_workers=max_workers,
        manual_base_uri=manual_base_uri,
    )
    sniper.load_map(map_path)

    if blast:
        print(f"\n[BLAST] Immediate scrape for {contract} (supply ~{supply})")
        sniper.blast_now()
        # If user only wanted the blast (common for manual front-run), exit unless they passed --watch
        if "--watch" not in sys.argv:
            print("Blast complete. Exiting.")
            print("Add --watch if you also want to keep the listener running.")
            return

    print(f"\nStarting live watcher (mode='{mode}') ...")
    try:
        asyncio.run(sniper.watch_contract(mode=mode))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
