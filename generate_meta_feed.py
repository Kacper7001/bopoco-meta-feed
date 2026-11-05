"""Generuje feed produktowy dla katalogu Meta z Shopify Admin API (tylko odczyt).

Kazdy aktywny wariant to jeden wiersz, a warianty produktu sa spiete przez
item_group_id. Wszystkie zdjecia z karty produktu trafiaja do kazdego wariantu:
pierwsze do image_link, reszta do additional_image_link.

Wynik: jeden plik bopoco_meta_feed.csv w katalogu --out.

id to numeryczne ID wariantu Shopify, takie samo jak w eksporcie katalogu.
Nie wolno go zmieniac: po nim Meta laczy produkt z dotychczasowa historia.

Token: zmienna SHOPIFY_ADMIN_TOKEN, a gdy jej nie ma, plik ../.shopify_token.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SHOP = "bopoco.myshopify.com"
API_URL = f"https://{SHOP}/admin/api/2025-10/graphql.json"
BRAND = "BOPOCO"
CURRENCY = "PLN"
MAX_ADDITIONAL_IMAGES = 20
MAX_TITLE = 200
MAX_DESCRIPTION = 9999
MAX_MATERIAL = 200
# Mniej aktywnych produktow niz tyle oznacza awarie, a nie decyzje sklepu.
MIN_PRODUCTS = 500

QUERY = """
query Feed($cursor: String) {
  products(first: 50, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title description vendor productType onlineStoreUrl
      images(first: 30) { nodes { url } }
      metafields(first: 30) { nodes { namespace key value } }
      variants(first: 100) {
        nodes { id title price compareAtPrice availableForSale }
      }
    }
  }
}
"""

FIELDS = [
    "id", "item_group_id", "title", "description", "availability", "condition",
    "price", "sale_price", "link", "image_link", "additional_image_link",
    "brand", "size", "color", "material", "gender", "age_group", "product_type",
    "google_product_category",
]


def load_token():
    token = os.environ.get("SHOPIFY_ADMIN_TOKEN", "").strip()
    if token:
        return token
    return (Path(__file__).resolve().parent.parent / ".shopify_token").read_text().strip()


def graphql(token, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    for attempt in range(5):
        request = urllib.request.Request(API_URL, body, headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise
        errors = payload.get("errors")
        if errors:
            throttled = any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errors)
            if throttled and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Shopify API: {errors}")
        return payload["data"]
    raise RuntimeError("Shopify API: wyczerpano ponowienia")


def fetch_products(token):
    products, cursor = [], None
    while True:
        page = graphql(token, QUERY, {"cursor": cursor})["products"]
        products.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return products
        cursor = page["pageInfo"]["endCursor"]


def numeric_id(gid):
    return gid.rsplit("/", 1)[-1]


def rich_text_to_plain(value):
    """Metapola rich text sa JSON-em; zwykly tekst przechodzi bez zmian."""
    try:
        node = json.loads(value)
    except (TypeError, ValueError):
        return value or ""

    def walk(item):
        if item.get("type") == "text":
            return item.get("value", "")
        return " ".join(walk(child) for child in item.get("children", []))

    return walk(node) if isinstance(node, dict) else str(node)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def build_description(product, material):
    description = clean(product["description"])
    if description:
        return description[:MAX_DESCRIPTION]
    # Meta wymaga opisu, a wiekszosc produktow go nie ma. Sklejamy go tylko
    # z danych sklepu, zeby niczego nie dopisywac do produktu.
    fallback = clean(product["title"])
    if material:
        fallback += f". Skład: {material}"
    return fallback[:MAX_DESCRIPTION]


def size_of(variant_title):
    if variant_title == "Default Title":
        return "Jeden rozmiar"
    return variant_title.replace("\\", "/")


def build_rows(products):
    rows = []
    skipped = []
    for product in products:
        images = [image["url"] for image in product["images"]["nodes"]]
        if not images or not product["onlineStoreUrl"]:
            skipped.append(product["title"])
            continue

        fields = {(m["namespace"], m["key"]): m["value"] for m in product["metafields"]["nodes"]}
        material = clean(rich_text_to_plain(fields.get(("custom", "szegoly_materialu"), "")))
        google_category = next(
            (value for (_, key), value in fields.items() if key == "google_product_category"), ""
        )
        product_id = numeric_id(product["id"])
        description = build_description(product, material)

        for variant in product["variants"]["nodes"]:
            variant_id = numeric_id(variant["id"])
            price = float(variant["price"])
            compare_at = float(variant["compareAtPrice"] or 0)
            on_sale = compare_at > price
            rows.append({
                "id": variant_id,
                "item_group_id": product_id,
                "title": clean(product["title"])[:MAX_TITLE],
                "description": description,
                "availability": "in stock" if variant["availableForSale"] else "out of stock",
                "condition": "new",
                "price": f"{compare_at if on_sale else price:.2f} {CURRENCY}",
                "sale_price": f"{price:.2f} {CURRENCY}" if on_sale else "",
                "link": f"{product['onlineStoreUrl']}?variant={variant_id}",
                "image_link": images[0],
                "additional_image_link": ",".join(images[1:1 + MAX_ADDITIONAL_IMAGES]),
                "brand": BRAND,
                "size": size_of(variant["title"]),
                "color": clean(fields.get(("custom", "kolor"), "")),
                "material": material[:MAX_MATERIAL],
                "gender": "female",
                "age_group": "adult",
                "product_type": clean(product["productType"]),
                "google_product_category": google_category,
            })
    return rows, skipped


def write_csv(path, rows):
    # Zapis przez plik tymczasowy, zeby blad nie zostawil polowy feedu.
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "dist"))
    args = parser.parse_args()

    products = fetch_products(load_token())
    if len(products) < MIN_PRODUCTS:
        sys.exit(f"Tylko {len(products)} produktow (minimum {MIN_PRODUCTS}); feed nie zostal nadpisany.")

    rows, skipped = build_rows(products)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "bopoco_meta_feed.csv", rows)

    print(f"Produkty: {len(products)}, warianty w feedzie: {len(rows)}, pominiete: {len(skipped)}")
    for title in skipped:
        print(f"  pominiety: {title}")


if __name__ == "__main__":
    main()
