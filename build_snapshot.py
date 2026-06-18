#!/usr/bin/env python3
"""
Build and persist an accurate full-collection rarity map.

Usage:
    python build_snapshot.py <contract_address> [max_items]

Example:
    python build_snapshot.py 0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d 10000

This is CRITICAL for a real rarity sniper.
You run this once (or daily) for a collection to get true trait frequencies.
Then your watcher and scans will use accurate scores instead of sample-based guesses.
"""
import os
import sys
from dotenv import load_dotenv
from rarity_utils import AlchemyNFTClient, fetch_full_rarity_map

load_dotenv()

def main():
    if len(sys.argv) < 2:
        print("Usage: python build_snapshot.py <contract> [max_supply]")
        print("Example: python build_snapshot.py 0x... 10000")
        sys.exit(1)

    contract = sys.argv[1].strip().lower()
    max_supply = int(sys.argv[2]) if len(sys.argv) > 2 else 10000

    api_key = os.getenv("ALCHEMY_API_KEY")
    if not api_key:
        print("ERROR: ALCHEMY_API_KEY required in .env")
        sys.exit(1)

    client = AlchemyNFTClient(api_key)
    rarity_map, count = fetch_full_rarity_map(client, contract, max_supply=max_supply, save=True)

    if not rarity_map:
        print("Failed to build rarity map. Check contract and API key.")
        sys.exit(1)

    print(f"\nDone. Rarity map built from {count} tokens.")
    print(f"Map saved to rarity_maps/{contract}.json")
    print("You can now use this for fast + accurate scoring in watcher / sniper.")

if __name__ == "__main__":
    main()
