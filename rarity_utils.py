"""
Shared utilities for accurate NFT rarity calculation and metadata fetching.

Key for pre-OpenSea sniping:
- Build a FULL collection trait frequency map once (or periodically).
- Use that map to score newly minted tokens accurately.
- Fast single-token metadata fetch via Alchemy.
"""
import json
import os
import time
import requests
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional, Tuple, Callable

# Default IPFS gateways (order matters for fallback)
DEFAULT_IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
]


def normalize_trait(trait: Dict) -> Tuple[Optional[str], Any]:
    """Normalize different metadata attribute formats."""
    if not isinstance(trait, dict):
        return None, None
    ttype = trait.get("trait_type") or trait.get("name")
    value = trait.get("value")
    if ttype is None or value is None:
        return None, None
    # Convert numbers etc to str for consistency
    if isinstance(value, (int, float)):
        value = str(value)
    return str(ttype), value


def build_trait_rarity_map(assets: List[Dict]) -> Dict[str, Dict[Any, float]]:
    """
    Build frequency map from list of assets.
    Returns {trait_type: {value: frequency (0-1)}}
    """
    trait_count = defaultdict(Counter)
    total = len(assets)
    if total == 0:
        return {}

    for asset in assets:
        traits = asset.get("traits") or []
        for trait in traits:
            ttype, value = normalize_trait(trait)
            if ttype and value is not None:
                trait_count[ttype][value] += 1

    rarity_map: Dict[str, Dict[Any, float]] = {}
    for ttype, values in trait_count.items():
        rarity_map[ttype] = {val: count / total for val, count in values.items()}

    return rarity_map


def compute_rarity_score(asset: Dict, rarity_map: Dict[str, Dict[Any, float]]) -> float:
    """Classic Rarity Score: sum(1 / freq) for each trait."""
    score = 0.0
    traits = asset.get("traits") or []
    for trait in traits:
        ttype, value = normalize_trait(trait)
        if ttype in rarity_map and value in rarity_map[ttype]:
            freq = rarity_map[ttype][value]
            if freq > 0:
                score += 1.0 / freq
    return round(score, 2)


def save_rarity_map(rarity_map: Dict, contract: str, total_supply: int, path: str = "rarity_maps"):
    """Save rarity map + metadata to disk for reuse."""
    os.makedirs(path, exist_ok=True)
    filepath = os.path.join(path, f"{contract.lower()}.json")
    data = {
        "contract": contract.lower(),
        "total_supply": total_supply,
        "generated_at": int(time.time()),
        "rarity_map": rarity_map,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return filepath


def load_rarity_map(contract: str, path: str = "rarity_maps") -> Optional[Dict]:
    """Load previously saved rarity map."""
    filepath = os.path.join(path, f"{contract.lower()}.json")
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


class AlchemyNFTClient:
    """Fast Alchemy NFT client focused on accuracy + speed for sniping."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "nft-rarity-sniper/2.0",
        })
        self.base = f"https://eth-mainnet.g.alchemy.com/nft/v2/{api_key}"

    def fetch_single_metadata(self, contract_address: str, token_id: str, refresh: bool = True) -> Optional[Dict]:
        """
        Get metadata for ONE token extremely fast.
        Returns normalized dict with token_id, name, traits, image, etc.
        """
        params = {
            "contractAddress": contract_address,
            "tokenId": token_id,
            "refreshCache": "true" if refresh else "false",
        }
        url = f"{self.base}/getNFTMetadata"
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            metadata = data.get("metadata") or {}
            traits = metadata.get("attributes") or metadata.get("traits") or []

            token_id_clean = str(token_id)
            if isinstance(token_id_clean, str) and token_id_clean.startswith("0x"):
                token_id_clean = str(int(token_id_clean, 16))

            return {
                "token_id": token_id_clean,
                "name": metadata.get("name") or f"#{token_id_clean}",
                "traits": traits,
                "image": metadata.get("image") or data.get("image", {}).get("cachedUrl"),
                "raw": data,  # keep raw for debugging
            }
        except Exception as e:
            print(f"[Alchemy] Single metadata error for {token_id}: {e}")
            return None

    def fetch_collection_metadata(
        self,
        contract_address: str,
        max_items: int = 10000,
        page_size: int = 100,
        with_metadata: bool = True,
    ) -> List[Dict]:
        """
        Paginate through entire collection.
        Returns list of normalized assets suitable for rarity calculation.
        WARNING: Large collections (10k) can take time + many calls.
        """
        assets = []
        next_token = None
        params = {
            "contractAddress": contract_address,
            "withMetadata": "true" if with_metadata else "false",
            "limit": str(page_size),
        }

        while len(assets) < max_items:
            if next_token:
                params["startToken"] = next_token

            url = f"{self.base}/getNFTsForCollection"
            try:
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()

                page = data.get("nfts", [])
                if not page:
                    break

                for nft in page:
                    metadata = nft.get("metadata") or {}
                    token_id = nft.get("id", {}).get("tokenId", "")
                    if token_id.startswith("0x"):
                        token_id = str(int(token_id, 16))

                    traits = metadata.get("attributes") or metadata.get("traits") or []

                    assets.append({
                        "token_id": token_id,
                        "name": metadata.get("name") or f"#{token_id}",
                        "traits": traits,
                    })

                    if len(assets) >= max_items:
                        break

                next_token = data.get("nextToken")
                if not next_token:
                    break

                time.sleep(0.15)  # be nice to API
            except Exception as e:
                print(f"[Alchemy] Collection fetch error: {e}")
                break

        return assets

    def resolve_ipfs(self, uri: str, gateways: Optional[List[str]] = None) -> str:
        """Convert ipfs:// or /ipfs/ links to http gateway URL."""
        if not uri:
            return ""
        if uri.startswith("http"):
            return uri
        if uri.startswith("ipfs://"):
            cid = uri[7:]
            gateways = gateways or DEFAULT_IPFS_GATEWAYS
            return gateways[0] + cid
        if "/ipfs/" in uri:
            return uri  # already gateway-ish
        return uri

    def rpc_call(self, method: str, params: list) -> Optional[Dict]:
        """Low-level JSON-RPC call using the same Alchemy key (for eth_getTransactionByHash etc)."""
        url = f"https://eth-mainnet.g.alchemy.com/v2/{self.api_key}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        try:
            resp = self.session.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            return data.get("result")
        except Exception as e:
            print(f"[Alchemy RPC] {method} error: {e}")
            return None

    def fetch_transaction(self, tx_hash: str) -> Optional[Dict]:
        """Fetch full transaction (to inspect input data for baseURI etc)."""
        return self.rpc_call("eth_getTransactionByHash", [tx_hash])


def fetch_full_rarity_map(
    alchemy_client: AlchemyNFTClient,
    contract_address: str,
    max_supply: int = 10000,
    save: bool = True,
) -> Tuple[Dict[str, Dict[Any, float]], int]:
    """
    High level helper: fetch lots of NFTs, build + optionally persist rarity map.
    Returns (rarity_map, num_tokens_fetched)
    """
    print(f"Fetching up to {max_supply} tokens for {contract_address} to build accurate rarity map...")
    assets = alchemy_client.fetch_collection_metadata(
        contract_address, max_items=max_supply
    )
    print(f"Fetched {len(assets)} tokens.")

    rarity_map = build_trait_rarity_map(assets)
    if save and rarity_map:
        filepath = save_rarity_map(rarity_map, contract_address, len(assets))
        print(f"Saved rarity map → {filepath}")

    return rarity_map, len(assets)


def fetch_direct_json(url: str, timeout: int = 8) -> Optional[Dict]:
    """Fetch raw JSON metadata from a direct http(s) or resolved IPFS URL."""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "nft-rarity-sniper-direct/1.0"})
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        # print(f"[Direct] Failed {url[:80]}...: {e}")
        return None


def _single_fetch_worker(args):
    """Internal worker for parallel fetch."""
    client, contract, tid, refresh = args
    return client.fetch_single_metadata(contract, str(tid), refresh=refresh)


def fetch_metadata_range_fast(
    client: AlchemyNFTClient,
    contract_address: str,
    start: int,
    end: int,
    max_workers: int = 60,
    refresh: bool = True,
    progress: bool = True,
) -> List[Dict]:
    """
    Scrape a range of token IDs **very fast** using parallel requests.
    Critical for front-running reveals — fetch hundreds/thousands right after the reveal tx.
    """
    token_ids = list(range(start, end + 1))
    results = []
    failed = 0

    print(f"[FAST SCRAPE] Fetching tokens {start}-{end} ({len(token_ids)} items) with {max_workers} workers...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_single_fetch_worker, (client, contract_address, tid, refresh)): tid
            for tid in token_ids
        }

        for i, future in enumerate(as_completed(futures), 1):
            tid = futures[future]
            try:
                data = future.result()
                if data and data.get("traits"):
                    results.append(data)
                else:
                    failed += 1
            except Exception:
                failed += 1

            if progress and i % 100 == 0:
                print(f"  ... {i}/{len(token_ids)} attempted | got {len(results)} with traits")

    print(f"[FAST SCRAPE] Done. Got {len(results)} tokens with traits. {failed} without/failed.")
    return results


def scrape_reveal_and_compute(
    client: AlchemyNFTClient,
    contract_address: str,
    supply: int = 10000,
    start_token: int = 0,
    max_workers: int = 80,
    build_and_save_map: bool = True,
    base_uri: Optional[str] = None,
) -> List[Dict]:
    """
    THE key function for front-running reveals.

    - Scrapes using Alchemy + refreshCache (very reliable)
    - If base_uri is provided, ALSO does parallel direct fetches (fastest path)
    - Builds accurate rarity map from the just-revealed data
    - Scores and returns ranked list (rarest first)
    """
    assets: List[Dict] = []

    # Always do fast Alchemy scrape (with refresh)
    print("[REVEAL] Starting Alchemy parallel scrape (refreshCache)...")
    alchemy_assets = fetch_metadata_range_fast(
        client,
        contract_address,
        start=start_token,
        end=start_token + supply - 1,
        max_workers=max_workers,
        refresh=True,
        progress=True,
    )
    assets.extend(alchemy_assets)

    # If we have a base_uri (extracted from tx or passed in), do direct parallel scrape too
    direct_assets = []
    if base_uri:
        print(f"[REVEAL] Base URI detected! Doing direct parallel scrape: {base_uri[:80]}...")
        direct_assets = fetch_direct_range_fast(base_uri, start_token, start_token + supply - 1, max_workers=max_workers)
        print(f"[REVEAL] Direct scrape got {len(direct_assets)} additional/updated items.")
        # Merge, prefer direct if it has traits
        seen = {a["token_id"] for a in assets}
        for da in direct_assets:
            if da.get("traits") and da["token_id"] not in seen:
                assets.append(da)
            elif da.get("traits"):
                # replace if direct has better data
                for idx, a in enumerate(assets):
                    if a["token_id"] == da["token_id"]:
                        assets[idx] = da
                        break

    if not assets:
        print("[REVEAL] No metadata scraped. Reveal may not be live yet or wrong supply.")
        return []

    unique_assets = {a["token_id"]: a for a in assets}.values()
    assets = list(unique_assets)

    print(f"[REVEAL] Building rarity from {len(assets)} freshly scraped tokens...")
    rarity_map = build_trait_rarity_map(assets)

    if build_and_save_map:
        try:
            save_rarity_map(rarity_map, contract_address, len(assets))
            print("[REVEAL] Saved fresh rarity map.")
        except Exception as e:
            print(f"[REVEAL] Could not save map: {e}")

    # Score every asset
    scored = []
    for asset in assets:
        score = compute_rarity_score(asset, rarity_map)
        asset["rarity_score"] = score
        scored.append(asset)

    ranked = sorted(scored, key=lambda x: x.get("rarity_score", 0), reverse=True)
    return ranked


def _direct_fetch_worker(args):
    base_uri, tid, gateways = args
    return fetch_single_direct(base_uri, tid, gateways)


def fetch_direct_range_fast(
    base_uri: str,
    start: int,
    end: int,
    max_workers: int = 60,
    gateways: Optional[List[str]] = None,
) -> List[Dict]:
    """Parallel direct fetch using the raw base URI (fastest possible when available)."""
    token_ids = list(range(start, end + 1))
    results = []

    print(f"[DIRECT SCRAPE] Hitting base URI directly for {len(token_ids)} tokens...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_direct_fetch_worker, (base_uri, tid, gateways)): tid
            for tid in token_ids
        }
        for i, future in enumerate(as_completed(futures), 1):
            try:
                data = future.result()
                if data and data.get("traits"):
                    results.append(data)
            except Exception:
                pass
            if i % 200 == 0:
                print(f"  [DIRECT] {i}/{len(token_ids)} ... got {len(results)}")

    return results


def get_top_rare_from_reveal(ranked: List[Dict], top_n: int = 30) -> List[Dict]:
    """Helper to extract top N and format nicely."""
    return ranked[:top_n]


# -------------------------------
# Direct / raw metadata scraping (maximum speed, bypass indexers)
# -------------------------------

def resolve_ipfs(uri: str, gateways: Optional[List[str]] = None) -> str:
    """Standalone IPFS resolver."""
    if not uri:
        return ""
    if uri.startswith("http"):
        return uri
    if uri.startswith("ipfs://"):
        cid = uri[7:]
        gws = gateways or DEFAULT_IPFS_GATEWAYS
        return gws[0] + cid
    if "/ipfs/" in uri:
        return uri
    return uri


def extract_base_uri_from_calldata(input_hex: str) -> Optional[str]:
    """
    Heuristic extractor for baseURI from transaction input data.
    Works for common functions like setBaseURI(string), reveal() that sets a URI, etc.
    Looks for strings that look like URIs (contain http, ipfs, /, .json patterns).
    """
    if not input_hex or not input_hex.startswith("0x"):
        return None
    try:
        data = bytes.fromhex(input_hex[2:])
    except Exception:
        return None

    # Look for potential string offsets and lengths in ABI-encoded data
    candidates = []
    i = 4  # skip selector
    while i + 64 <= len(data):
        try:
            # Try to interpret as offset
            offset = int.from_bytes(data[i:i+32], "big")
            if 0 < offset < len(data):
                # Read length
                if offset + 32 <= len(data):
                    length = int.from_bytes(data[offset:offset+32], "big")
                    if 10 < length < 500:  # reasonable URI length
                        start = offset + 32
                        end = start + length
                        if end <= len(data):
                            raw = data[start:end]
                            try:
                                s = raw.decode("utf-8", errors="ignore").strip("\x00")
                                if any(x in s.lower() for x in ["http", "ipfs", "/", ".json", "arweave"]):
                                    candidates.append(s)
                            except:
                                pass
        except:
            pass
        i += 32

    # Also brute force scan for long printable strings that look like URIs
    current = ""
    for b in data:
        if 32 <= b <= 126:
            current += chr(b)
        else:
            if len(current) > 15 and any(k in current.lower() for k in ["http", "ipfs", "arweave", "/ipfs/"]):
                candidates.append(current)
            current = ""
    if len(current) > 15 and any(k in current.lower() for k in ["http", "ipfs"]):
        candidates.append(current)

    # Return the longest plausible one
    candidates = [c for c in candidates if len(c) > 10]
    if candidates:
        return sorted(candidates, key=len, reverse=True)[0]
    return None


def fetch_single_direct(base_uri: str, token_id: int, gateways: Optional[List[str]] = None) -> Optional[Dict]:
    """
    Try to fetch metadata JSON directly from a revealed baseURI + tokenId.
    This can be the absolute fastest path if you know (or extract) the new base URI.
    """
    base = base_uri.rstrip("/")
    candidates = [
        f"{base}/{token_id}.json",
        f"{base}/{token_id}",
    ]

    for raw_url in candidates:
        url = resolve_ipfs(raw_url, gateways)
        data = fetch_direct_json(url)
        if data and (data.get("attributes") or data.get("traits")):
            return {
                "token_id": str(token_id),
                "name": data.get("name") or f"#{token_id}",
                "traits": data.get("attributes") or data.get("traits") or [],
                "image": data.get("image"),
                "raw": data,
            }
    return None
