# BD Products Scraper

Built by [Talha Pythoneer](https://www.talhapythoneer.com), web scraping and AI agents.

Scrapes the BD (Becton, Dickinson) US product catalog at bd.com/en-us. It covers every product page listed in the official product sitemap (about 8,270 SKUs), downloads the product images locally, and writes a nested JSON file. A second script flattens that JSON into a CSV.

## What it collects (per product)

| Field | Source on page |
|---|---|
| `sku`, `gtin`, `title` | Header block |
| `brand`, `category`, `product_family`, `meta_description` | Page metadata |
| `image_urls`, `images[].local_path` | Product gallery, full-resolution originals |
| `overview_text`, `overview_html` | Overview tab, cleaned to plain semantic HTML |
| `features_and_benefits` | Overview tab cards |
| `references_html` | Overview tab references / indications |
| `specifications` | Specifications tab: every group and attribute (GTIN rows include the units-per-package quantity) |
| `faq` | FAQ tab (only some product families have one) |
| `related_products` | Related Products tab (SKU + title) |

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
```

## Usage

```bash
# evenly spread sample across the whole catalog
python bd_scraper.py --sample 50 --out bd_products_sample
python flatten_to_csv.py output/bd_products_sample.json

# full catalog (resumable: rerun the same command after an interruption)
python bd_scraper.py --workers 3 --delay 1.5
python flatten_to_csv.py output/bd_products.json

# specific SKUs
python bd_scraper.py --skus 5626240 305106 --out custom
```

Progress is checkpointed to `output/<name>.progress.jsonl`, so a rerun skips products that are already done. Images go to `output/images/<SKU>/<SKU>_01.png` and so on. The JSON stores those paths relative to `output/`.

## Output

JSON (`output/bd_products.json`), with placeholder values:

```json
{
  "total_products": 1,
  "products": [{
    "url": "https://www.bd.com/en-us/products-and-solutions/products/product-page.XXXXXXX",
    "sku": "XXXXXXX",
    "gtin": "00000000000000",
    "title": "Example Catheter 13 Fr",
    "brand": "Example™",
    "category": "Example category",
    "images": [{"url": "https://www.bd.com/content/dam/.../example.png",
                "local_path": "images/XXXXXXX/XXXXXXX_01.png"}],
    "overview_html": "<ul><li>Feature one</li></ul>",
    "specifications": [
      {"group": "GTIN", "attributes": [{"name": "GTIN - Case", "value": "10000000000000", "quantity": "5"}]},
      {"group": "Packaging", "attributes": [{"name": "Package", "value": "5/shelfpack, 50/case"}]}
    ],
    "faq": [{"question": "...", "answer": "..."}]
  }]
}
```

A 50-product sample of real output (JSON + CSV) is in [sample/](sample/). Images are not included in the repo; running the scraper downloads them into `output/images/`.

CSV (`output/bd_products.csv`): one row per product. It has fixed columns, and each spec attribute found anywhere in the data gets its own `Group: Attribute` column (for example `GTIN: GTIN - Case`, `GTIN: GTIN - Case (Qty)`, `Packaging: Package`). Multi-value cells use ` | ` as the separator.

## Notes

- The site sits behind Akamai Bot Manager. Plain `requests`/`curl` get HTTP 403, so the scraper uses `curl_cffi` with a Chrome TLS fingerprint. It reuses sessions, retries with backoff, and keeps the request rate low. Bursts trigger temporary connection resets.
- Products without a gallery show a generic placeholder on the site. Those get `images: []` rather than a downloaded placeholder.
- Image requests send an `Accept` header for PNG/JPEG. Without it, the CDN converts images to AVIF.
