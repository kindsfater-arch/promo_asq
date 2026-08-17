#!/usr/bin/env python3
"""Загрузка страниц банков через Playwright и превращение их в чистый текст.

Пять сайтов из шести отдают готовый SSR-HTML и парсились бы обычным curl,
но у Альфа-Банка стоит JS-челлендж ServicePipe: без браузера приходит
полтора килобайта со спиннером. Поэтому браузер один на всех.

    python src/fetch.py                    # все банки
    python src/fetch.py --bank alfa        # один банк
    python src/fetch.py --banks vtb,tbank  # несколько
    python src/fetch.py --stale            # только отставшие (см. selection.py)
"""

from __future__ import annotations

import argparse
import gzip
import re
import ssl
import sys
import time
from datetime import date
from html import unescape
from http.client import HTTPException
from pathlib import Path
from typing import List
from urllib.request import Request, urlopen

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import selection  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "data" / "snapshots"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Сбер, Альфа, ВТБ и БСПБ отдают TLS-цепочку, замкнутую на корень
# «Russian Trusted Root CA» Минцифры. На ноутбуке он стоит в системном
# хранилище, и оттуда сбор проходит; на раннере GitHub его нет, и с 03.08
# по 17.08 эти четыре банка не собрались там ни разу за 51 попытку —
# ERR_CERT_AUTHORITY_INVALID у браузера, «self-signed certificate in
# certificate chain» у запасного пути.
#
# Бандл лежит в репозитории и ДОБАВЛЯЕТСЯ к системным корням, а не
# заменяет их: проверка сертификата остаётся включённой везде, ГПБ и
# Т-Банк со своими обычными CA проверяются ровно как раньше.
CA_BUNDLE = ROOT / "certs" / "russian_trusted_ca.pem"

# Chromium системным хранилищем OpenSSL не пользуется, поэтому те же
# сертификаты передаются ему отдельно — списком SPKI-отпечатков. Флаг
# снимает ошибку цепочки ТОЛЬКО для сертификатов с этими открытыми
# ключами, для всех остальных сайтов проверка обычная.
#
# Нужны отпечатки всех трёх, а не одного корня: сервер присылает корень
# не всегда. Сбер присылает и корень — и прошёл сразу; ВТБ шлёт только
# лист и промежуточный, поэтому без строки 2024 года он продолжал падать
# с ERR_CERT_AUTHORITY_INVALID. Промежуточные носят одно имя «Russian
# Trusted Sub CA», но ключи у них разные, и банки стоят на выпуске 2024.
#
# Пересчитать отпечаток из бандла:
#   openssl x509 -in <файл> -pubkey -noout | openssl pkey -pubin -outform der \
#     | openssl dgst -sha256 -binary | base64
CHROMIUM_ARGS = [
    "--ignore-certificate-errors-spki-list="
    "ArgiDAcHKNt3HZrFnlRSHE7drSGng7smz98ZwdsPrjc="   # Root CA
    ",BEeqSxjEi56NsW6RgJKG3Sfv1qULqA0whOuecLqOHco="  # Sub CA, выпуск 2022
    ",N7la4XONxMaWYRRHFWaf25yT562zNjQi3rYcDzrPZcE="  # Sub CA, выпуск 2024
]

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


# Запасной путь без браузера. ВТБ и Т-Банк периодически рвут соединение
# с Chromium на раннере GitHub (net::ERR_CONNECTION_CLOSED) — так их
# защита отвечает на трафик из чужого дата-центра. Обычный HTTPS-запрос
# из стандартной библиотеки идёт с другим TLS-отпечатком и иногда
# проходит там, где браузер получает разрыв. Обе страницы серверные,
# так что для них JS не нужен; Альфе этот путь не поможет, у неё
# челлендж, и она честно упрётся в проверку looks_broken.
PLAIN_TIMEOUT = 30
PLAIN_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, identity",
    "Connection": "close",
}

BLOCK_TAGS = re.compile(
    r"<\s*(script|style|noscript|svg|iframe|template)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.I | re.S,
)
LINE_BREAKS = re.compile(r"<\s*(br|/p|/div|/li|/h[1-6]|/tr|/section)\b[^>]*>", re.I)
ANY_TAG = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """Грубое превращение HTML в текст — на уровне innerText, но без DOM."""
    html = BLOCK_TAGS.sub(" ", html)
    html = LINE_BREAKS.sub("\n", html)
    return unescape(ANY_TAG.sub(" ", html))


def ssl_context() -> ssl.SSLContext:
    """Системные корни плюс корень Минцифры, если бандл на месте."""
    ctx = ssl.create_default_context()
    if CA_BUNDLE.is_file():
        ctx.load_verify_locations(cafile=str(CA_BUNDLE))
    else:
        print(f"  (нет {CA_BUNDLE.name} — банки на сертификатах Минцифры "
              f"не соберутся)", file=sys.stderr)
    return ctx


def fetch_plain(name: str, url: str) -> str:
    """Последняя попытка: HTTPS-запрос без браузера."""
    try:
        with urlopen(Request(url, headers=PLAIN_HEADERS), timeout=PLAIN_TIMEOUT,
                     context=ssl_context()) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            charset = resp.headers.get_content_charset() or "utf-8"
    # URLError, HTTPError и разрывы соединения — все наследники OSError;
    # HTTPException ловит обрыв на середине чтения, он мимо OSError.
    except (OSError, HTTPException, ValueError) as exc:
        print(f"  … {name}: запрос без браузера не прошёл — {exc}")
        return ""

    text = clean(html_to_text(raw.decode(charset, "replace")))
    problem = looks_broken(text)
    if problem:
        print(f"  … {name}: запрос без браузера дал {problem}")
        return ""
    print(f"  ✓ {name}: {len(text)} символов (без браузера)")
    return text


def rel(path: Path) -> Path:
    """Путь покороче для вывода; каталог вне проекта печатаем как есть."""
    try:
        return path.resolve().relative_to(ROOT)
    except ValueError:
        return path


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

    print(f"  ✗ {name}: браузером не удалось — {last}", file=sys.stderr)
    return fetch_plain(name, url)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    selection.add_args(ap)
    ap.add_argument("--out", type=Path, default=None,
                    help="каталог снапшотов (по умолчанию data/snapshots/<дата>)")
    args = ap.parse_args()

    banks: List[dict] = selection.select(selection.load_banks(), args)
    if not banks:
        print("Обновлять нечего: у всех банков свежие данные")
        return 0

    out_dir = args.out or SNAP_DIR / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    failed = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=CHROMIUM_ARGS)
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

    print(f"\nСнимки: {rel(out_dir)} — "
          f"{len(banks) - len(failed)} из {len(banks)}")
    if failed:
        print(f"Не собраны: {', '.join(failed)}", file=sys.stderr)
    # Ненулевой код только если не собрали вообще ничего: частичный сбой
    # штатно обрабатывает validate.py, помечая банк как stale.
    return 1 if len(failed) == len(banks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
