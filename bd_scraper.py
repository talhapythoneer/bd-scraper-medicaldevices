"""
BD (bd.com, en-us) product scraper.

Reads every product URL from the official product sitemap, scrapes each product
page (SKU, GTIN, title, brand/category/family, images, overview, specifications,
FAQ, related products), downloads product images locally and writes a JSON file.

Usage:
    python bd_scraper.py --sample 50          # evenly spread sample of 50 products
    python bd_scraper.py                      # full run (all products in sitemap)
    python bd_scraper.py --skus 5626240 305106
"""

import argparse
import json
import logging
import random
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment
from curl_cffi import requests

BASE = "https://www.bd.com"
SITEMAP_URL = f"{BASE}/en-us/sitemap_products.xml"
PRODUCT_URL = f"{BASE}/en-us/products-and-solutions/products/product-page.{{sku}}"
EXAMPLE_SKU = "5626240"  # product the client referenced

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output"
IMG_DIR = OUT_DIR / "images"

log = logging.getLogger("bd")
_local = threading.local()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session(impersonate="chrome", timeout=60)
    return _local.s


def get(url, accept=None, retries=5):
    headers = {"Accept": accept} if accept else None
    for attempt in range(1, retries + 1):
        try:
            r = session().get(url, headers=headers)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return r
            log.warning("HTTP %s on %s (attempt %d)", r.status_code, url, attempt)
        except Exception as e:  # network / TLS resets from the CDN
            log.warning("%s on %s (attempt %d)", type(e).__name__, url, attempt)
            _local.__dict__.pop("s", None)  # fresh connection next time
        time.sleep(min(60, 2 ** attempt) + random.random())
    raise RuntimeError(f"Failed after {retries} attempts: {url}")


def sitemap_urls():
    xml = get(SITEMAP_URL).text
    return re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def clean(text):
    if text is None:
        return None
    text = text.replace(" ", " ").replace(" ", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def txt(el):
    return clean(el.get_text(" ")) if el else None


KEEP_TAGS = {"p", "ul", "ol", "li", "strong", "b", "em", "i", "u", "sup", "sub", "br",
             "a", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td"}
BLOCK_TAGS = {"p", "ul", "ol", "li", "h2", "h3", "h4", "h5", "h6", "table", "tr"}


def sanitize_html(el):
    """Reduce an AEM fragment to plain semantic HTML (no classes, divs or scripts)."""
    el = BeautifulSoup(str(el), "lxml")
    for t in el.find_all(["script", "style", "noscript", "svg", "img", "button", "iframe"]):
        t.decompose()
    for c in el.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for t in el.find_all(True):
        if t.name not in KEEP_TAGS:
            t.unwrap()
            continue
        href = t.get("href") if t.name == "a" else None
        t.attrs = {}
        if href:
            t["href"] = urljoin(BASE, href)
    # drop trailing <br> inside blocks and empty block elements
    for br in el.find_all("br"):
        if br.parent and br.parent.name in BLOCK_TAGS and not "".join(
                str(s) for s in br.next_siblings).strip():
            br.decompose()
    for t in el.find_all(BLOCK_TAGS):
        if not t.get_text(strip=True):
            t.decompose()
    html = "".join(str(c) for c in (el.body or el).contents)
    html = html.replace(" ", " ").replace(" ", " ")
    html = re.sub(r"\s*\n\s*", " ", html)
    html = re.sub(r"\s{2,}", " ", html)
    html = re.sub(r">\s+<", "><", html)
    html = re.sub(r"(<(?:p|li|h\d|td|th)>)\s+", r"\1", html)
    html = re.sub(r"\s+(</(?:p|li|h\d|td|th|a)>)", r"\1", html)
    return html.strip() or None


def drop_noise(root):
    """Remove hidden AEM flags ('true'/'false') and 'NOT AVAILABLE' placeholders."""
    for h in root.select(".bd-container__hidden"):
        h.decompose()
    # the feature/benefit cards are rendered twice (grid + mobile carousel)
    for c in root.select(".bd-card-carousel-container__show-carousel"):
        c.decompose()
    for s in root.find_all(string=re.compile(r"NOT AVAILABLE")):
        s.extract()
    for t in root.select(".component-heading"):
        t.decompose()


def full_size(src):
    """Gallery images are served with ?resize=...; strip it for the original asset."""
    return urljoin(BASE, src).split("?")[0]


# --------------------------------------------------------------------------- #
# Product page
# --------------------------------------------------------------------------- #
def parse_product(html, url):
    soup = BeautifulSoup(html, "lxml")
    if not soup.select_one("h1.bd-product-details__heading"):
        return None

    ld = {}
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or "", strict=False)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            ld = data
            break

    sku = txt(soup.select_one(".bd-product-details__sku-code")) or ld.get("sku")
    gtin = txt(soup.select_one(".bd-product-details__gtin-code")) or ld.get("gtin")

    # ---- images (gallery only; pages without one show a generic placeholder)
    images = []
    for img in soup.select("#product-gallery__carousel .carousel-item img"):
        src = img.get("src") or img.get("data-src")
        if src:
            u = full_size(src)
            if u not in images:
                images.append(u)
    if not images:
        images = [full_size(u) for u in ld.get("image") or []]

    # ---- overview tab
    overview_text = overview_html = None
    features, references_html = [], None
    ov_tab = soup.select_one("#product-tab-overview")
    if ov_tab:
        drop_noise(ov_tab)
        for card in ov_tab.select(".bd-feature-benefit__container"):
            head = txt(card.select_one(".bd-feature-benefit__header"))
            desc = txt(card.select_one(".bd-feature-benefit__description"))
            if head or desc:
                features.append({"heading": head, "description": desc})
        refs = ov_tab.select_one("#product-FairBalance")
        if refs and refs.get_text(strip=True):
            references_html = sanitize_html(refs)
            refs.decompose()
        main = ov_tab.select_one("#product-Overview")
        if main and main.get_text(strip=True):
            for t in main.select(".bd-product-overview__title"):
                t.name = "h3"
            overview_html = sanitize_html(main)
            overview_text = txt(main)

    # ---- specifications tab (grouped accordions)
    specifications = []
    for item in soup.select("#product-Specification .accordion-item"):
        group = txt(item.select_one(".cmp-accordion__title"))
        attrs = []
        for row in item.select(".bd-table__container"):
            name = txt(row.select_one(".bd-table__heading"))
            vals = [txt(v) for v in row.select(".bd-table__description")]
            if not name:
                continue
            attr = {"name": name, "value": vals[0] if vals else None}
            if len(vals) > 1:  # GTIN rows: second value = units in that package level
                attr["quantity"] = vals[1]
            if len(vals) > 2:
                attr["extra_values"] = vals[2:]
            attrs.append(attr)
        if group or attrs:
            specifications.append({"group": group, "attributes": attrs})

    # ---- FAQ tab (only some product families have one)
    faq = []
    for it in soup.select("#product-tab-faq .cmp-accordion__item"):
        q = txt(it.select_one(".cmp-accordion__title"))
        panel = it.select_one(".cmp-accordion__panel")
        a = txt(panel)
        if q and a:
            faq.append({"question": q, "answer": a})

    # ---- related products
    related = []
    for li in soup.select("#product-tab-rp li.bd-relatedProducts__card"):
        r_sku = li.get("data-sku")
        if r_sku and all(r["sku"] != r_sku for r in related):
            related.append({"sku": r_sku, "title": clean(li.get("data-title"))})

    meta_desc = soup.find("meta", attrs={"name": "description"})

    return {
        "url": url,
        "sku": sku,
        "gtin": gtin,
        "title": txt(soup.select_one("h1.bd-product-details__heading")),
        "brand": txt(soup.select_one(".product-hidden-fields.productBrand")),
        "category": txt(soup.select_one(".product-hidden-fields.productCategory")),
        "product_family": txt(soup.select_one(".product-hidden-fields.productFamily")),
        "meta_description": clean(meta_desc.get("content")) if meta_desc else None,
        "image_urls": images,
        "images": [],  # filled in by download_images()
        "overview_text": overview_text,
        "overview_html": overview_html,
        "features_and_benefits": features,
        "references_html": references_html,
        "specifications": specifications,
        "faq": faq,
        "related_products": related,
    }


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
_img_cache = {}  # image url -> first local file (many SKUs share family images)
_img_lock = threading.Lock()

EXT_BY_TYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
               "image/webp": ".webp", "image/svg+xml": ".svg", "image/tiff": ".tif"}


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def download_images(product):
    sku = safe_name(product["sku"] or "unknown")
    out = []
    for i, url in enumerate(product["image_urls"], 1):
        stem = IMG_DIR / sku / f"{sku}_{i:02d}"
        with _img_lock:
            cached = _img_cache.get(url)
        existing = next(iter(stem.parent.glob(stem.name + ".*")), None)
        if existing:
            path = existing
        elif cached and cached.exists():
            path = stem.with_suffix(cached.suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cached, path)
        else:
            # Akamai Image Manager returns AVIF to browsers; ask for the original format
            r = get(url, accept="image/png,image/jpeg,image/gif;q=0.9,*/*;q=0.5")
            if r.status_code != 200:
                log.warning("image %s -> HTTP %s", url, r.status_code)
                continue
            ctype = r.headers.get("content-type", "").split(";")[0].strip()
            ext = EXT_BY_TYPE.get(ctype) or Path(urlsplit(url).path).suffix.lower() or ".bin"
            path = stem.with_suffix(ext)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
        with _img_lock:
            _img_cache.setdefault(url, path)
        out.append({"url": url, "local_path": path.relative_to(OUT_DIR).as_posix()})
    product["images"] = out
    return product


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def scrape_one(url, delay):
    time.sleep(delay * (0.5 + random.random()))
    r = get(url)
    if r.status_code == 404:
        return {"url": url, "error": "404"}
    product = parse_product(r.text, url)
    if product is None:
        return {"url": url, "error": "not a product page"}
    return download_images(product)


def load_done(jsonl):
    done = {}
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["url"]] = rec
    return done


def pick_sample(urls, n):
    """Client's example product + an even spread across the sitemap (covers all divisions)."""
    first = PRODUCT_URL.format(sku=EXAMPLE_SKU)
    rest = [u for u in urls if u != first]
    step = len(rest) / (n - 1)
    return [first] + [rest[int(i * step)] for i in range(n - 1)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, help="scrape an evenly spread sample of N products")
    ap.add_argument("--skus", nargs="+", help="scrape specific SKUs only")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--delay", type=float, default=1.5, help="avg seconds between requests per worker")
    ap.add_argument("--out", default="bd_products", help="output file stem inside ./output")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    OUT_DIR.mkdir(exist_ok=True)

    if args.skus:
        urls = [PRODUCT_URL.format(sku=s) for s in args.skus]
    else:
        urls = sitemap_urls()
        log.info("sitemap: %d product URLs", len(urls))
        if args.sample:
            urls = pick_sample(urls, args.sample)

    jsonl = OUT_DIR / f"{args.out}.progress.jsonl"
    done = load_done(jsonl)  # resume support
    todo = [u for u in urls if u not in done]
    log.info("%d to scrape (%d already done)", len(todo), len(done))

    lock = threading.Lock()
    with ThreadPoolExecutor(args.workers) as pool, jsonl.open("a", encoding="utf-8") as fh:
        futures = {pool.submit(scrape_one, u, args.delay): u for u in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            url = futures[fut]
            try:
                rec = fut.result()
            except Exception as e:
                log.error("FAILED %s: %s", url, e)
                continue
            with lock:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                done[url] = rec
            if n % 25 == 0 or n == len(todo):
                log.info("progress %d/%d", n, len(todo))

    order = {u: i for i, u in enumerate(urls)}
    products = sorted((r for u, r in done.items() if u in order and "error" not in r),
                      key=lambda r: order[r["url"]])
    errors = [r for u, r in done.items() if u in order and "error" in r]
    result = {
        "source": "https://www.bd.com/en-us (product sitemap)",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_products": len(products),
        "products": products,
    }
    out = OUT_DIR / f"{args.out}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("wrote %s (%d products, %d errors)", out, len(products), len(errors))
    for e in errors:
        log.warning("skipped %s: %s", e["url"], e["error"])


if __name__ == "__main__":
    main()
