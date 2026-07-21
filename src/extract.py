#!/usr/bin/env python3
"""Превращение текста страницы в структурированные данные через Gemini.

CSS-селекторы тут не годятся: банки перекраивают лендинги несколько раз
в год, и селектор ломается молча — отдаёт пустоту или чужое число.
Модель со строгой JSON-схемой переживает редизайн.

Провайдер целиком спрятан в extract_bank(). Замена модели — правка
одного файла; fetch/validate/build о нём не знают.

    export GEMINI_API_KEY=...
    python src/extract.py                     # все банки
    python src/extract.py --bank vtb          # один
    python src/extract.py --dry-run           # печать JSON без записи
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from schema import Bank  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "banks.json"
SNAP_DIR = ROOT / "data" / "snapshots"

# Версии зафиксированы намеренно: gemini-flash-latest молча переезжает
# на новое поколение, и извлечение поменялось бы без единой правки в коде.
# Модели пробуются по порядку — свежие вида 3.5-flash регулярно отдают
# 503 «high demand», и тогда работу доделывает предыдущее поколение.
# Список доступного:
#   curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"
# Проверено на этом ключе: 2.5-flash и 2.5-flash-lite закрыты для новых
# аккаунтов (404), у 2.0-flash нулевая бесплатная квота (429). Работают
# только эти две.
MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]

RETRIES = 3          # попыток на каждую модель
BACKOFF = 25         # секунд, удваивается с каждой попыткой

# Бесплатный тир ограничен ~20 запросами в минуту на модель. Шести банков
# это с запасом хватает, но только если не бить очередью: без паузы серия
# повторов забивает минутное окно и всё сваливается в 429.
PAUSE_BETWEEN_BANKS = 5

PROMPT = """Ты извлекаешь условия по торговому эквайрингу и СБП (Система быстрых
платежей) со страницы российского банка.

Банк: {name}
Страница: {url}
Сегодня: {today}

Заполни JSON строго по схеме. Правила:

1. Бери ТОЛЬКО то, что прямо написано на странице. Если значения нет — ставь null
   или пустой список. НИЧЕГО не додумывай и не переноси из общих знаний о банке.
2. В source_quote клади дословную цитату со страницы (10–200 символов), из которой
   видно значение. Цитата должна встречаться в тексте буквально, символ в символ.
   Если точной цитаты нет — ставь null и не заполняй само значение.
3. Ставки — числа в процентах: «0,7%» -> 0.7, «от 1,2%» -> 1.2, «0%» -> 0.
   Не путай ставку комиссии с процентами кешбэка, конверсии, скидки или НДС.
4. Даты — в формате YYYY-MM-DD. Если у акции указан только срок («3 месяца»,
   «6 месяцев»), это не дата: оставь valid_until null, а срок опиши словами
   в rate_note или conditions.
5. ended = true только если на странице прямо сказано, что акция завершена.
6. base_rate_from — минимальная базовая ставка торгового эквайринга вне акций.
7. sbp_vat_free: true — если банк прямо пишет, что НДС на комиссию по СБП
   не начисляется; false — если пишет, что начисляется; null — если не сказано.
8. Промо-акции — только про эквайринг, СБП и приём платежей. Вклады, кредиты,
   зарплатные проекты и кобрендовые карты игнорируй.
9. extras — неценовые преимущества (бесплатная аренда терминала, рассрочка,
   скорость зачисления). Не более 6 пунктов, каждый — короткая фраза.
10. notes — то, что выглядит противоречиво или требует ручной проверки
    (условия спрятаны в PDF, две разные ставки в разных местах страницы).

Текст страницы:
---
{text}
---
"""

# Поля, которые заполняет модель. Остальное (id, name, color, source_url,
# checked_at, status) проставляется нами и в схему для модели не идёт.
MODEL_FIELDS = [
    "base_rate_from", "base_rate_note", "base_rate_quote",
    "promos", "sbp_rates", "sbp_note", "vat_note", "sbp_vat_free",
    "extras", "notes",
]


def response_schema() -> dict:
    """JSON-схема для Gemini — из той же pydantic-модели, что и всё остальное."""
    full = Bank.model_json_schema()
    defs = full.get("$defs", {})

    def resolve(node):
        """Gemini не умеет $ref/anyOf — разворачиваем в плоскую схему."""
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].rsplit("/", 1)[-1]])
            if "anyOf" in node:  # Optional[X] -> X, nullable
                variants = [v for v in node["anyOf"] if v.get("type") != "null"]
                out = resolve(variants[0]) if variants else {"type": "string"}
                out["nullable"] = True
                if "description" in node:
                    out["description"] = node["description"]
                return out
            out = {}
            for key, val in node.items():
                # "title"/"default" выбрасываем как служебные ключи JSON-Schema,
                # но внутри "properties" это ИМЕНА полей (у Promo есть поле
                # title) — там чистить нельзя.
                if key == "properties":
                    out[key] = {k: resolve(v) for k, v in val.items()}
                    continue
                if key in ("title", "default", "$defs"):
                    continue
                out[key] = resolve(val)
            return out
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    props = {k: resolve(v) for k, v in full["properties"].items()
             if k in MODEL_FIELDS}
    return {
        "type": "object",
        "properties": props,
        "required": [k for k in MODEL_FIELDS if k in props],
    }


def extract_bank(text: str, name: str, url: str) -> dict:
    """Единственная точка, знающая про LLM-провайдера."""
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit(
            "Не задан GEMINI_API_KEY. Локально: export GEMINI_API_KEY=...\n"
            "В GitHub Actions — секрет репозитория с тем же именем."
        )

    client = genai.Client(api_key=api_key)
    prompt = PROMPT.format(
        name=name, url=url, today=date.today().isoformat(), text=text
    )
    config = types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json",
        response_schema=response_schema(),
    )

    last: Optional[Exception] = None
    for model in MODELS:
        for attempt in range(1, RETRIES + 1):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                if model != MODELS[0]:
                    print(f"    (через запасную модель {model})")
                return json.loads(resp.text)
            except Exception as exc:  # noqa: BLE001
                last = exc
                msg = str(exc)
                # 404 — модель недоступна этому ключу, повторять бессмысленно.
                if "NOT_FOUND" in msg or "404" in msg:
                    break
                # Перегрузка и обрывы связи лечатся ожиданием.
                if attempt < RETRIES:
                    pause = BACKOFF * (2 ** (attempt - 1))
                    print(f"    {model}: попытка {attempt} не удалась, "
                          f"пауза {pause} с")
                    time.sleep(pause)

    raise RuntimeError(f"все модели отказали, последняя ошибка: {last}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bank", help="id банка; по умолчанию все")
    ap.add_argument("--snapshots", type=Path, default=None,
                    help="каталог со снимками (по умолчанию сегодняшний)")
    ap.add_argument("--dry-run", action="store_true",
                    help="напечатать результат, ничего не записывая")
    ap.add_argument("--out", type=Path, default=None,
                    help="куда сложить сырой результат (по умолчанию рядом со снимками)")
    args = ap.parse_args()

    snap_dir = args.snapshots or SNAP_DIR / date.today().isoformat()
    if not snap_dir.is_dir():
        print(f"Нет снимков в {snap_dir}. Сначала запустите src/fetch.py",
              file=sys.stderr)
        return 2

    banks: List[Dict] = json.loads(DATA_FILE.read_text(encoding="utf-8"))["banks"]
    if args.bank:
        banks = [b for b in banks if b["id"] == args.bank]

    results, missing = {}, []
    for i, bank in enumerate(banks):
        if i:
            time.sleep(PAUSE_BETWEEN_BANKS)
        snap = snap_dir / f"{bank['id']}.txt"
        if not snap.is_file():
            print(f"  – {bank['id']}: снимка нет, пропуск")
            missing.append(bank["id"])
            continue
        text = snap.read_text(encoding="utf-8")
        try:
            data = extract_bank(text, bank["name"], bank["source_url"])
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 — падение одного банка не фатально
            print(f"  ✗ {bank['id']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            missing.append(bank["id"])
            continue
        results[bank["id"]] = data
        promos = len(data.get("promos") or [])
        print(f"  ✓ {bank['id']}: ставка {data.get('base_rate_from')}, "
              f"акций {promos}, СБП {len(data.get('sbp_rates') or [])}")

    if args.dry_run:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    out = args.out or snap_dir / "extracted.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ {out.relative_to(ROOT)} — {len(results)} из {len(banks)}")
    if missing:
        print(f"Без данных: {', '.join(missing)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
