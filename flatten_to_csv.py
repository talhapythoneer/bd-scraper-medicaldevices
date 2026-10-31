"""
Flatten the scraped BD JSON into a single CSV (one row per product).

Specification attributes differ per product, so their columns are discovered
from the data: every distinct "<Group>: <Attribute>" becomes a column, and GTIN
rows also get a "<Group>: <Attribute> (Qty)" column for the units per package.

Usage:
    python flatten_to_csv.py                                   # output/bd_products.json -> .csv
    python flatten_to_csv.py output/bd_products_sample.json
"""

import csv
import json
import sys
from pathlib import Path

SEP = " | "

BASE_COLUMNS = [
    "url", "sku", "gtin", "title", "brand", "category", "product_family",
    "meta_description", "image_count", "image_urls", "image_local_paths",
    "overview_text", "overview_html", "features_and_benefits", "references_html",
    "faq_count", "faq", "related_product_skus",
]


def spec_columns(product):
    cols = {}
    for group in product["specifications"]:
        g = group["group"] or "Other"
        for a in group["attributes"]:
            key = f"{g}: {a['name']}"
            n = 2
            while key in cols:  # same attribute name twice in one group
                key = f"{g}: {a['name']} ({n})"
                n += 1
            cols[key] = a["value"]
            if a.get("quantity") is not None:
                cols[f"{key} (Qty)"] = a["quantity"]
            if a.get("extra_values"):
                cols[f"{key} (Extra)"] = SEP.join(a["extra_values"])
    return cols


def row(p):
    r = {
        "url": p["url"],
        "sku": p["sku"],
        "gtin": p["gtin"],
        "title": p["title"],
        "brand": p["brand"],
        "category": p["category"],
        "product_family": p["product_family"],
        "meta_description": p["meta_description"],
        "image_count": len(p["images"]),
        "image_urls": SEP.join(i["url"] for i in p["images"]),
        "image_local_paths": SEP.join(i["local_path"] for i in p["images"]),
        "overview_text": p["overview_text"],
        "overview_html": p["overview_html"],
        "features_and_benefits": SEP.join(
            ": ".join(filter(None, (f["heading"], f["description"])))
            for f in p["features_and_benefits"]),
        "references_html": p["references_html"],
        "faq_count": len(p["faq"]),
        "faq": json.dumps(p["faq"], ensure_ascii=False) if p["faq"] else "",
        "related_product_skus": SEP.join(r["sku"] for r in p["related_products"]),
    }
    r.update(spec_columns(p))
    return r


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "output/bd_products.json")
    dst = src.with_suffix(".csv")
    products = json.loads(src.read_text(encoding="utf-8"))["products"]

    rows = [row(p) for p in products]
    spec_cols = []
    for r in rows:  # keep first-seen order so related columns stay together
        for k in r:
            if k not in BASE_COLUMNS and k not in spec_cols:
                spec_cols.append(k)
    # group spec columns by their spec group, preserving first-seen order within a group
    group_order = list(dict.fromkeys(c.split(": ", 1)[0] for c in spec_cols))
    spec_cols.sort(key=lambda c: group_order.index(c.split(": ", 1)[0]))

    # utf-8-sig so Excel shows ™/® correctly
    with dst.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=BASE_COLUMNS + spec_cols, restval="")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {dst}: {len(rows)} rows, {len(BASE_COLUMNS) + len(spec_cols)} columns "
          f"({len(spec_cols)} spec columns)")


if __name__ == "__main__":
    main()
