#!/usr/bin/env python3
"""Сборка дашборда: data/banks.json + шаблон -> docs/index.html.

HTML никогда не правится руками. Всё, что видно на странице, приезжает
отсюда, поэтому автообновление данных не расходится с текстом выводов.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, str(Path(__file__).parent))
from schema import Bank, Dataset  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "banks.json"
TEMPLATE_DIR = ROOT / "templates"
OUT_FILE = ROOT / "docs" / "index.html"

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


# --- форматирование -------------------------------------------------------

def pct(value: Optional[float]) -> str:
    """0.3 -> '0,3%', 0.0 -> '0%', None -> '—'."""
    if value is None:
        return "—"
    if value == int(value):
        return f"{int(value)}%"
    return f"{value}".replace(".", ",") + "%"


def ru_date(d: Optional[date]) -> str:
    return f"{d.day:02d}.{d.month:02d}.{d.year}" if d else "—"


def ru_date_long(d: Optional[date]) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year}" if d else "—"


def plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


# --- вычисляемые выводы ---------------------------------------------------

def build_insights(banks: List[Bank], today: date) -> List[dict]:
    """Ключевые выводы считаются из данных, а не пишутся в шаблоне.

    Иначе после первого же автообновления фраза «самая агрессивная акция —
    ВТБ, 0%» переживёт саму акцию и разойдётся с таблицей ниже.
    """
    insights: List[dict] = []

    rated = [b for b in banks if b.best_rate is not None]
    if rated:
        top = min(rated, key=lambda b: b.best_rate)
        promo = next(
            (p for p in top.active_promos if p.rate == top.best_rate), None
        )
        insights.append({
            "kicker": "Минимальная ставка",
            "value": pct(top.best_rate),
            "title": top.name,
            "body": (promo.rate_note if promo and promo.rate_note
                     else top.base_rate_note or "базовый тариф"),
            "color": top.color,
        })

    # Ближайший дедлайн среди действующих акций — то, что реально горит.
    deadlines = [
        (p.valid_until, b, p)
        for b in banks for p in b.active_promos
        if p.valid_until is not None
    ]
    if deadlines:
        until, bank, promo = min(deadlines, key=lambda x: x[0])
        days = (until - today).days
        insights.append({
            "kicker": "Ближайший дедлайн",
            "value": f"{days} {plural(days, 'день', 'дня', 'дней')}",
            "title": f"{bank.name} — {promo.title}",
            "body": f"Условия действуют до {ru_date_long(until)}",
            "color": bank.color,
        })

    sbp_banks = [b for b in banks if b.min_sbp_rate is not None]
    if sbp_banks and rated:
        cheapest_sbp = min(sbp_banks, key=lambda b: b.min_sbp_rate)
        base_rates = [b.base_rate_from for b in banks if b.base_rate_from is not None]
        insights.append({
            "kicker": "СБП против эквайринга",
            "value": f"{pct(cheapest_sbp.min_sbp_rate)} … {pct(max(b.max_sbp_rate for b in sbp_banks))}",
            "title": "СБП дешевле везде",
            "body": (
                f"Против {pct(min(base_rates))} … {pct(max(base_rates))} "
                f"по классическому эквайрингу"
            ),
            "color": cheapest_sbp.color,
        })

    no_vat = [b for b in banks if b.sbp_vat_free is True]
    if no_vat:
        insights.append({
            "kicker": "НДС на комиссию",
            "value": f"{len(no_vat)} из {len(banks)}",
            "title": "не начисляют НДС на СБП",
            "body": ", ".join(b.name for b in no_vat)
                    + ". У остальных комиссия по СБП облагается НДС по общим правилам.",
            "color": no_vat[0].color,
        })
    else:
        # Без этой ветки при отсутствии освобождённых банков в сетке
        # остаётся пустая ячейка.
        new_client = [b for b in banks
                      if any(p.audience for p in b.active_promos)]
        if new_client:
            insights.append({
                "kicker": "Промо новым клиентам",
                "value": f"{len(new_client)} из {len(banks)}",
                "title": "банков дают стартовые условия",
                "body": ", ".join(b.name for b in new_client),
                "color": new_client[0].color,
            })

    return insights[:4]


def annotate(banks: List[Bank], today: date) -> List[dict]:
    """Плоские записи для шаблона + флаги подсветки лучших значений."""
    rates = [b.best_rate for b in banks if b.best_rate is not None]
    sbps = [b.min_sbp_rate for b in banks if b.min_sbp_rate is not None]
    best_rate = min(rates) if rates else None
    best_sbp = min(sbps) if sbps else None

    rows = []
    for b in banks:
        active = b.active_promos
        headline = active[0] if active else None
        deadlines = [p.valid_until for p in active if p.valid_until]
        nearest = min(deadlines) if deadlines else None
        rows.append({
            "bank": b,
            "headline": headline,
            "active": active,
            "ended": b.ended_promos,
            "deadline": nearest,
            "days_left": (nearest - today).days if nearest else None,
            "is_best_rate": b.best_rate is not None and b.best_rate == best_rate,
            "is_best_sbp": b.min_sbp_rate is not None and b.min_sbp_rate == best_sbp,
            "sbp_range": (
                pct(b.min_sbp_rate) if b.min_sbp_rate == b.max_sbp_rate
                else f"{pct(b.min_sbp_rate)} … {pct(b.max_sbp_rate)}"
            ) if b.min_sbp_rate is not None else "—",
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DATA_FILE)
    ap.add_argument("--out", type=Path, default=OUT_FILE)
    args = ap.parse_args()

    dataset = Dataset(**json.loads(args.data.read_text(encoding="utf-8")))
    today = date.today()

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = pct
    env.filters["ru_date"] = ru_date
    env.filters["ru_date_long"] = ru_date_long
    # «6 банка» в шапке резало глаз — склоняем по числу.
    env.globals["plural"] = plural

    html = env.get_template("index.html.j2").render(
        rows=annotate(dataset.banks, today),
        insights=build_insights(dataset.banks, today),
        generated_at=dataset.generated_at,
        published_at=datetime.now(),
        today=today,
        stale=[b for b in dataset.banks if b.status == "stale"],
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    try:
        shown = args.out.resolve().relative_to(ROOT)
    except ValueError:  # --out указали вне каталога проекта
        shown = args.out
    print(f"✓ {shown} — {len(html) // 1024} КБ, {len(dataset.banks)} банков")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
