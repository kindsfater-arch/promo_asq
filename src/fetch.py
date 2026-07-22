#!/usr/bin/env python3
"""Загрузка страниц банков через Playwright и превращение их в чистый текст.

Пять сайтов из шести отдают готовый SSR-HTML и парсились бы обычным curl,
но у Альфа-Банка стоит JS-челлендж ServicePipe: без браузера приходит
полтора килобайта со спиннером. Поэтому браузер один на всех.

    python src/fetch.py                # все банки
    python src/fetch.py --bank alfa    # один банк
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Dict, List

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "banks.json"
SNAP_DIR = ROOT / "data" / "snapshots"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Т-Банк просит Crawl-delay: 2.0 в robots.txt — соблюдаем для всех.
POLITE_DELAY = 2.0
NAV_TIMEOUT = 60_000
SETTLE_MS = 5_000   # пауза после networkidle на дорисовку главного экрана
MAX_CHARS = 40_000
ATTEMPTS = 3

# Признаки того, что вместо страницы приехала антибот-заглушка.
CHALLENGE_MARKERS = ("js-challenge", "servicepipe", "captcha_frame")

# Ключевые слова тематики: если их нет, страница почти наверняка не та.
TOPIC_MARKERS = ("эквайринг", "сбп", "комисс", "тариф", "платеж")

# Из DOM вырезаем только то, что заведомо не несёт текста.
#
# ВАЖНО: header/nav/footer здесь НЕ трогаем, хотя соблазн велик. Сбер
# держит главный оффер («Ставка от 0,3% со счётом для бизнеса») внутри
# <header>, и вместе с меню вырезалась сама ключевая цифра — модель
# видела только 1% из юридической сноски внизу страницы. Лишние пункты
# меню в тексте дешевле, чем потерянная ставка.
EXTRACT_JS = """
() => {
  const drop = 'script,style,noscript,svg,iframe,template,' +
               '[class*="cookie"],[class*="Cookie"],[id*="cookie"]';
  const doc = document.body.cloneNode(true);
  doc.querySelectorAll(drop).forEach(el => el.remove());
  return doc.innerText;
}
"""


def clean(text: str) -> str:
    """Схлопывает пустые строки и повторы — экономит токены на извлечении."""
    lines, seen_blank = [], False
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            if not seen_blank and lines:
                lines.append("")
            seen_blank = True
            continue
        seen_blank = False
        lines.append(line)
    return "\n".join(lines)[:MAX_CHARS]


def looks_broken(text: str) -> str:
    """Возвращает причину, по которой снимок непригоден, или пустую строку."""
    low = text.lower()
    if len(text) < 800:
        return f"слишком короткая страница ({len(text)} символов)"
    if any(m in low for m in CHALLENGE_MARKERS):
        return "антибот-заглушка вместо контента"
    if not any(m in low for m in TOPIC_MARKERS):
        return "на странице нет ключевых слов по теме эквайринга"
    return ""


def fetch_one(page, name: str, url: str) -> str:
    """Тянет одну страницу, повторяя попытки при челлендже и таймауте."""
    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            # ServicePipe у Альфы решает челлендж и сам делает редирект —
            # networkidle тут ненадёжен, ждём появления осмысленного текста.
            try:
                page.wait_for_function(
                    "() => document.body && document.body.innerText.length > 2000",
                    timeout=25_000,
                )
            except PWTimeout:
                pass
            # Порог в 2000 символов набирается быстро — меню и подвал
            # приезжают первыми. Главный экран с ключевой ставкой у Сбера
            # дорисовывается заметно позже, поэтому ждём затишья в сети
            # и добавляем паузу: без неё в снимок попадал только 1% из
            # юридической сноски, а «Ставка от 0,3%» терялась.
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except PWTimeout:
                pass
            page.wait_for_timeout(SETTLE_MS)
            text = clean(page.evaluate(EXTRACT_JS))
            problem = looks_broken(text)
            if not problem:
                print(f"  ✓ {name}: {len(text)} символов (попытка {attempt})")
                return text
            last = problem
            print(f"  … {name}: {problem}, повтор {attempt}/{ATTEMPTS}")
        except (PWTimeout, PWError) as exc:
            last = str(exc).splitlines()[0]
            print(f"  … {name}: {last}, повтор {attempt}/{ATTEMPTS}")
        time.sleep(POLITE_DELAY * attempt)

    print(f"  ✗ {name}: не удалось — {last}", file=sys.stderr)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bank", help="id банка; по умолчанию все")
    ap.add_argument("--out", type=Path, default=None,
                    help="каталог снапшотов (по умолчанию data/snapshots/<дата>)")
    args = ap.parse_args()

    banks: List[Dict] = json.loads(DATA_FILE.read_text(encoding="utf-8"))["banks"]
    if args.bank:
        banks = [b for b in banks if b["id"] == args.bank]
        if not banks:
            print(f"Банк «{args.bank}» не найден в data/banks.json", file=sys.stderr)
            return 2

    out_dir = args.out or SNAP_DIR / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    failed = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(
            user_agent=UA,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        for i, bank in enumerate(banks):
            if i:
                time.sleep(POLITE_DELAY)
            text = fetch_one(page, bank["id"], bank["source_url"])
            if text:
                (out_dir / f"{bank['id']}.txt").write_text(text, encoding="utf-8")
            else:
                failed.append(bank["id"])
        browser.close()

    print(f"\nСнимки: {out_dir.relative_to(ROOT)} — "
          f"{len(banks) - len(failed)} из {len(banks)}")
    if failed:
        print(f"Не собраны: {', '.join(failed)}", file=sys.stderr)
    # Ненулевой код только если не собрали вообще ничего: частичный сбой
    # штатно обрабатывает validate.py, помечая банк как stale.
    return 1 if len(failed) == len(banks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
