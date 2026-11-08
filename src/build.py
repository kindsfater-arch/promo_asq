#!/usr/bin/env python3
"""Сборка дашборда: data/banks.json + шаблон -> docs/index.html.

HTML никогда не правится руками: всё, что видно на странице, приезжает
из data/banks.json через шаблон.
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


def plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


# --- подготовка строк для шаблона ------------------------------------------

def get_terminal_info(b: Bank) -> str:
    text = " ".join(b.extras + b.notes).lower()
    if b.id == "bspb":
        return "0 ₽ (от 300 000 ₽/мес) / 1 000 ₽"
    if b.id == "sber":
        return "0 ₽ первые 3 мес."
    if any(k in text for k in ("бесплатная аренда", "бесплатно в рамках", "0 ₽", "без арендной")):
        return "0 ₽ (бесплатно)"
    return "0 ₽ / в тарифе"


def get_settlement_speed(b: Bank) -> str:
    text = " ".join(b.extras).lower()
    if "до 5 раз" in text:
        return "До 5 раз в день"
    if "до двух раз" in text or "до 2 раз" in text:
        return "До 2 раз в день"
    if "день в день" in text:
        return "День в день"
    if "следующий день" in text:
        return "На следующий день"
    return "1 рабочий день"


def annotate(banks: List[Bank], today: date) -> List[dict]:
    """Плоские записи для шаблона + флаги подсветки лучших значений."""
    rates = [b.best_rate for b in banks if b.best_rate is not None]
    best_rate = min(rates) if rates else None

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
            "terminal_info": get_terminal_info(b),
            "settlement_speed": get_settlement_speed(b),
            "has_promos": len(active) > 0,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DATA_FILE)
    ap.add_argument("--out", type=Path, default=OUT_FILE)
    args = ap.parse_args()

    dataset = Dataset(**json.loads(args.data.read_text(encoding="utf-8")))
    today = date.today()

    active_promos_count = sum(len(b.active_promos) for b in dataset.banks)
    rates = [b.best_rate for b in dataset.banks if b.best_rate is not None]
    min_rate = min(rates) if rates else None
    free_terminals_count = sum(
        1 for b in dataset.banks
        if any(k in " ".join(b.extras + b.notes).lower() for k in ("бесплатн", "0 ₽", "без арендн"))
    )
    stats = {
        "min_rate": min_rate,
        "promos_count": active_promos_count,
        "free_terminals_count": free_terminals_count,
        "total_banks": len(dataset.banks),
    }

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = pct
    env.filters["ru_date"] = ru_date
    # «6 банка» в шапке резало глаз — склоняем по числу.
    env.globals["plural"] = plural

    banks_payload = dataset.model_dump(mode="json")["banks"]

    html = env.get_template("index.html.j2").render(
        rows=annotate(dataset.banks, today),
        stats=stats,
        banks_json=json.dumps(banks_payload, ensure_ascii=False),
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
