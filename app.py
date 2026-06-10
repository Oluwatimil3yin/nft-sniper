import os
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from collections import Counter, defaultdict
import requests
import time

load_dotenv()

app = Flask(__name__)

OPENSEA_API_URL = "https://api.opensea.io/api/v1/assets"
OPENSEA_COLLECTION_URL = "https://api.opensea.io/api/v1/collection/{slug}"
ALCHEMY_NFTS_FOR_COLLECTION = "https://eth-mainnet.g.alchemy.com/nft/v2/{api_key}/getNFTsForCollection"
OPENSEA_ASSET_LINK = "https://opensea.io/assets/{contract}/{token_id}"


class OpenSeaClient:
    def __init__(self, api_key=None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "nft-rarity-sniper-web/1.0",
            }
        )
        if api_key:
            self.session.headers["X-API-KEY"] = api_key

    def fetch_collection_stats(self, slug):
        url = OPENSEA_COLLECTION_URL.format(slug=slug)
        resp = self.session.get(url, timeout=20)
        resp.raise_for_status()
        return resp.json().get("collection", {}).get("stats", {})

    def fetch_assets(self, collection_slug, limit=50):
        assets = []
        params = {
            "collection": collection_slug,
            "limit": 50,
        }
        next_cursor = None

        while len(assets) < limit:
            if next_cursor:
                params["cursor"] = next_cursor

            resp = self.session.get(OPENSEA_API_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            page_assets = data.get("assets", [])
            if not page_assets:
                break

            assets.extend(page_assets)
            next_cursor = data.get("next")
            if not next_cursor:
                break

            time.sleep(0.25)

        return assets[:limit]


class AlchemyClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "nft-rarity-sniper-web/1.0",
            }
        )

    def fetch_assets(self, contract_address, limit=50):
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

            url = ALCHEMY_NFTS_FOR_COLLECTION.format(api_key=self.api_key)
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            page_assets = data.get("nfts", [])
            if not page_assets:
                break

            for nft in page_assets:
                asset = self._normalize_nft(nft, contract_address)
                assets.append(asset)
                if len(assets) >= limit:
                    break

            next_token = data.get("nextToken")
            if not next_token:
                break

            time.sleep(0.25)

        return assets[:limit]

    def _normalize_nft(self, nft, contract_address):
        metadata = nft.get("metadata") or {}
        token_id = nft.get("id", {}).get("tokenId")
        if token_id and token_id.startswith("0x"):
            token_id = str(int(token_id, 16))

        return {
            "token_id": token_id,
            "name": metadata.get("name") or f"#{token_id}",
            "traits": metadata.get("attributes") or metadata.get("traits") or [],
            "permalink": OPENSEA_ASSET_LINK.format(contract=contract_address, token_id=token_id),
            "sell_orders": [],
            "last_sale": None,
        }


def build_trait_rarity_map(assets):
    trait_count = defaultdict(Counter)
    total = len(assets)

    for asset in assets:
        for trait in asset.get("traits", []):
            trait_type = trait.get("trait_type")
            value = trait.get("value")
            if trait_type and value is not None:
                trait_count[trait_type][value] += 1

    rarity_map = {}
    for trait_type, values in trait_count.items():
        rarity_map[trait_type] = {
            value: count / total for value, count in values.items()
        }

    return rarity_map


def compute_rarity_score(asset, rarity_map):
    score = 0.0
    for trait in asset.get("traits", []):
        trait_type = trait.get("trait_type")
        value = trait.get("value")
        if trait_type in rarity_map and value in rarity_map[trait_type]:
            freq = rarity_map[trait_type][value]
            if freq > 0:
                score += 1.0 / freq
    return round(score, 2)


def parse_price(asset):
    sell_orders = asset.get("sell_orders") or []
    if sell_orders:
        order = sell_orders[0]
        base_price = int(order.get("base_price", 0))
        decimals = int(order.get("payment_token_contract", {}).get("decimals", 18))
        return base_price / (10 ** decimals)

    if asset.get("last_sale"):
        total_price = int(asset["last_sale"].get("total_price", 0))
        decimals = int(asset["last_sale"].get("payment_token", {}).get("decimals", 18))
        return total_price / (10 ** decimals)

    return None


def format_asset(asset, score, floor_price):
    asset_id = asset.get("token_id")
    name = asset.get("name") or f"#{asset_id}"
    permalink = asset.get("permalink") or ""
    price = parse_price(asset)
    price_text = f"{price:.4f} ETH" if price is not None else "n/a"
    gap_text = "n/a"
    if price is not None and floor_price is not None:
        gap_text = f"{price - floor_price:.4f} ETH"
    return {
        "name": name,
        "token_id": asset_id,
        "score": score,
        "price": price,
        "price_text": price_text,
        "gap_text": gap_text,
        "permalink": permalink,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/snipe", methods=["POST"])
def snipe():
    data = request.json
    collection = data.get("collection")
    contract = data.get("contract")
    limit = int(data.get("limit", 50))
    top = int(data.get("top", 10))

    try:
        if collection:
            opensea_key = os.getenv("OPENSEA_API_KEY")
            client = OpenSeaClient(api_key=opensea_key)
            assets = client.fetch_assets(collection, limit=limit)
            if not assets:
                return jsonify({"error": "No assets found. Check the collection slug."}), 400

            stats = client.fetch_collection_stats(collection)
            floor_price = stats.get("floor_price")
        else:
            alchemy_key = os.getenv("ALCHEMY_API_KEY")
            if not alchemy_key:
                return jsonify({"error": "ALCHEMY_API_KEY not configured."}), 400

            client = AlchemyClient(api_key=alchemy_key)
            assets = client.fetch_assets(contract, limit=limit)
            if not assets:
                return jsonify({"error": "No NFTs found. Check the contract address."}), 400

            floor_price = None

        rarity_map = build_trait_rarity_map(assets)
        scored_assets = []

        for asset in assets:
            score = compute_rarity_score(asset, rarity_map)
            scored_assets.append((score, asset))

        scored_assets.sort(key=lambda x: x[0], reverse=True)
        candidates = [
            format_asset(asset, score, floor_price)
            for score, asset in scored_assets[:top]
        ]

        return jsonify({
            "source": "opensea" if collection else "alchemy",
            "floor_price": floor_price,
            "candidates": candidates,
            "total_scanned": len(assets),
        })

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"API error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
