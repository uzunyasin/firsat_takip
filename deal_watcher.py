#!/usr/bin/env python3
"""
ucuzaucak.net firsat takipcisi.

/ucak-biletleri/ sayfasini kontrol eder, daha once gorulmemis biletleri
tespit eder ve Telegram uzerinden bildirir.

Secicileri denemek icin (bildirim gondermez):
    python deal_watcher.py --test

Telegram ayarlarini dogrulamak icin (deneme mesaji atar):
    python deal_watcher.py --ping
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


def env(name: str, default: str = "") -> str:
    """Bos string'i de 'tanimlanmamis' sayar (Actions bos degisken gonderir)."""
    return os.environ.get(name) or default


# --- Ayarlar ----------------------------------------------------------------

SITE_URL = env("SITE_URL", "https://ucuzaucak.net/ucak-biletleri/")

# Firsat linkleri hep /ucak-bileti/ kalibinda. Tema degisse bile bozulmaz.
# (Liste sayfasi /ucak-biletleri/ oldugu icin sondaki egik cizgi onemli.)
ITEM_SELECTOR = env("ITEM_SELECTOR", 'a[href*="/ucak-bileti/"]')
TITLE_SELECTOR = env("TITLE_SELECTOR")   # bos: kartin kendi metni
LINK_SELECTOR = env("LINK_SELECTOR")     # bos: kartin kendi href'i
PRICE_SELECTOR = env("PRICE_SELECTOR")   # bu sitede fiyat zaten baslikta

# Baslik sonundaki "3 gun once yayinlandi" kismini temizler.
TITLE_STRIP = env("TITLE_STRIP", r"\s*\d+\s+\S+\s+önce yayınlandı\s*$")

STATE_FILE = Path(env("STATE_FILE", "seen.json"))
MAX_STATE_ENTRIES = 2000
MAX_NOTIFY_ITEMS = 25
TELEGRAM_LIMIT = 3500

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}


# --- Veri cekme -------------------------------------------------------------

def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def clean_title(text: str) -> str:
    if TITLE_STRIP:
        text = re.sub(TITLE_STRIP, "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_deals(page_html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    deals: dict[str, dict] = {}

    for card in soup.select(ITEM_SELECTOR):
        # Kartin kendisi bir <a> ise href'i dogrudan ondan al.
        if not LINK_SELECTOR and card.name == "a":
            href = card.get("href")
        else:
            link_el = card.select_one(LINK_SELECTOR or "a")
            href = link_el.get("href") if link_el else None

        title_el = card.select_one(TITLE_SELECTOR) if TITLE_SELECTOR else None
        title = clean_title((title_el or card).get_text(" ", strip=True))
        if not title:
            continue

        link = urljoin(base_url, href) if href else base_url

        price = ""
        if PRICE_SELECTOR:
            price_el = card.select_one(PRICE_SELECTOR)
            if price_el:
                price = price_el.get_text(" ", strip=True)

        # Kimlik icin link tercih edilir; baslik zamanla degisebiliyor.
        raw_id = link if href else title
        deal_id = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:16]

        # Ayni firsat sayfada birden fazla link olarak cikabiliyor
        # ("Incele" butonu, baslik, ay etiketleri). En aciklayici
        # metne sahip olani tutuyoruz.
        existing = deals.get(deal_id)
        if existing and len(existing["title"]) >= len(title):
            continue

        deals[deal_id] = {
            "id": deal_id,
            "title": title[:300],
            "link": link,
            "price": price,
        }

    return list(deals.values())


# --- Durum yonetimi ---------------------------------------------------------

def load_seen() -> list[str]:
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_seen(ids: list[str]) -> None:
    STATE_FILE.write_text(
        json.dumps(ids[-MAX_STATE_ENTRIES:], ensure_ascii=False, indent=0),
        encoding="utf-8",
    )


# --- Telegram ---------------------------------------------------------------

def _telegram_post(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Telegram hatasi {resp.status_code}: {resp.text}")


def send_telegram(deals: list[dict]) -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    shown = deals[:MAX_NOTIFY_ITEMS]
    extra = len(deals) - len(shown)

    header = f"✈️ <b>{len(deals)} yeni fırsat</b>"
    lines = []
    for d in shown:
        title = html.escape(d["title"])
        price = f" — <b>{html.escape(d['price'])}</b>" if d["price"] else ""
        lines.append(f'• <a href="{html.escape(d["link"])}">{title}</a>{price}')
    if extra > 0:
        lines.append(f"… ve {extra} fırsat daha")

    chunks: list[str] = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > TELEGRAM_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}"
    chunks.append(current)

    for chunk in chunks:
        _telegram_post(token, chat_id, chunk)


# --- Ana akis ---------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="Sadece bulunanlari yazdir, bildirim gonderme")
    parser.add_argument("--ping", action="store_true",
                        help="Telegram ayarlarini test et")
    args = parser.parse_args()

    if args.ping:
        _telegram_post(os.environ["TELEGRAM_TOKEN"],
                       os.environ["TELEGRAM_CHAT_ID"],
                       "✅ Fırsat takipçisi bağlantısı çalışıyor.")
        print("Deneme mesaji gonderildi.")
        return 0

    deals = parse_deals(fetch_html(SITE_URL), SITE_URL)
    print(f"Sayfada {len(deals)} firsat bulundu.")

    if args.test:
        for d in deals:
            print(f"  - {d['title'][:90]}")
            print(f"    {d['link']}")
        if not deals:
            print("  (Bos -> ITEM_SELECTOR'u kontrol et)")
        return 0

    if not deals:
        # Site yapisi degismis olabilir; Actions kirmizi yansin ki haberin olsun.
        print("HATA: Hic firsat parse edilemedi.", file=sys.stderr)
        return 1

    seen = load_seen()
    new_deals = [d for d in deals if d["id"] not in set(seen)]

    if not seen:
        print("Ilk calisma, mevcut firsatlar taban olarak kaydediliyor.")
        save_seen([d["id"] for d in deals])
        return 0

    if not new_deals:
        print("Yeni firsat yok.")
        return 0

    print(f"{len(new_deals)} yeni firsat -> Telegram'a gonderiliyor.")
    send_telegram(new_deals)
    save_seen(seen + [d["id"] for d in new_deals])
    return 0


if __name__ == "__main__":
    sys.exit(main())
