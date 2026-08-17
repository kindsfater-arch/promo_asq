#!/usr/bin/env python3
"""Кто идёт в прогон: все банки или только те, что отстали.

Одно правило на всех. `fetch.py`, `extract.py`, `validate.py` и шаг
планирования в Actions спрашивают список здесь, иначе точечное
дообновление и полный прогон разъезжаются: собрали один набор банков,
извлекли другой, записали третий.

Банк требует обновления, если:

* статус не `ok` — прошлый прогон его не добрал (страницу не отдали,
  модель не разобрала, приёмка забраковала всё содержание);
* данные старше STALE_AFTER_DAYS — страховка на случай, когда прогона
  не было вовсе: статусы тогда остаются `ok`, а данные тихо стареют.

Только стандартная библиотека: в Actions файл запускается до установки
зависимостей, чтобы пустое дообновление не тратило минуты на playwright.

    python src/selection.py                  # id отставших, через запятую
    python src/selection.py --all            # id всех банков
    python src/selection.py --ids vtb,tbank  # проверить и нормализовать список
    python src/selection.py --report         # таблица для сводки прогона
    python src/selection.py --check-age 7    # ненулевой код, если данные протухли
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "banks.json"

# Полный прогон идёт в понедельник и четверг, то есть нормальный разрыв
# между обновлениями — максимум 4 дня. Данные старше означают, что прогон
# не состоялся целиком, и банк надо забрать дообновлением.
STALE_AFTER_DAYS = 4


def load_banks(path: Path = DATA_FILE) -> List[Dict]:
    return json.loads(path.read_text(encoding="utf-8"))["banks"]


def checked_on(bank: Dict) -> Optional[date]:
    try:
        return datetime.fromisoformat(str(bank.get("checked_at"))[:10]).date()
    except (TypeError, ValueError):
        return None


def age_days(bank: Dict, today: date) -> Optional[int]:
    seen = checked_on(bank)
    return None if seen is None else (today - seen).days


def too_old(bank: Dict, today: date, max_age: int) -> bool:
    """Данные старше max_age дней или с неразобранной датой проверки."""
    age = age_days(bank, today)
    return age is None or age > max_age


def needs_refresh(bank: Dict, today: date, max_age: int = STALE_AFTER_DAYS) -> bool:
    if bank.get("status") != "ok":
        return True
    return too_old(bank, today, max_age)


def parse_ids(raw: Optional[str]) -> List[str]:
    """«vtb,tbank» и «vtb tbank» — одно и то же."""
    if not raw:
        return []
    return [part for part in raw.replace(",", " ").split() if part]


def check_ids(banks: List[Dict], ids: List[str]) -> List[str]:
    known = {b["id"] for b in banks}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise SystemExit(
            f"Неизвестные id банков: {', '.join(unknown)}. "
            f"Есть: {', '.join(sorted(known))}"
        )
    return ids


def add_args(parser: argparse.ArgumentParser) -> None:
    """Общие для fetch/extract/validate ключи выбора банков."""
    group = parser.add_argument_group("выбор банков")
    group.add_argument("--banks", metavar="ID,ID", help="только эти банки")
    group.add_argument("--bank", metavar="ID", help="то же для одного банка")
    group.add_argument(
        "--stale", action="store_true",
        help=f"только отставшие: статус не ok или данные старше "
             f"{STALE_AFTER_DAYS} дней",
    )


def select(banks: List[Dict], args: argparse.Namespace,
           today: Optional[date] = None) -> List[Dict]:
    """Подмножество банков по ключам командной строки.

    Явный список сильнее `--stale`: если человек назвал банк руками, он
    хочет именно его, даже если данные по нему свежие.
    """
    today = today or date.today()
    ids = parse_ids(getattr(args, "banks", None)) + parse_ids(getattr(args, "bank", None))
    if ids:
        check_ids(banks, ids)
        return [b for b in banks if b["id"] in ids]
    if getattr(args, "stale", False):
        return [b for b in banks if needs_refresh(b, today)]
    return list(banks)


def report(banks: List[Dict], today: date) -> str:
    """Markdown-таблица: одинаково читается в терминале и в сводке Actions."""
    lines = [
        "| банк | статус | данные от | возраст | |",
        "|---|---|---|---|---|",
    ]
    for b in banks:
        age = age_days(b, today)
        age_txt = "—" if age is None else f"{age} дн."
        mark = "⚠️ в очередь на дообновление" if needs_refresh(b, today) else "свежие"
        lines.append(
            f"| {b['name']} | {b.get('status')} | {b.get('checked_at')} | "
            f"{age_txt} | {mark} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="все банки, а не только отставшие")
    ap.add_argument("--ids", metavar="ID,ID",
                    help="проверить, что такие банки есть, и вывести их список")
    ap.add_argument("--report", action="store_true", help="таблица состояния данных")
    ap.add_argument("--check-age", type=int, metavar="ДНЕЙ",
                    help="код 1, если у какого-то банка данные старше указанного")
    args = ap.parse_args()

    today = date.today()
    banks = load_banks()

    if args.report:
        print(report(banks, today))
        return 0

    if args.check_age is not None:
        # Считает too_old(), а не выражение `age_days() or 10**6`, которое
        # стояло здесь раньше: у банка, проверенного сегодня, возраст 0, а
        # 0 в питоне ложь — и подстановка превращала самый свежий банк в
        # протухший на миллион дней. Шаг «Возраст данных» падал ровно
        # после удачного полного прогона, когда все шесть банков только
        # что обновились, и GitHub слал письмо о несуществующем сбое.
        old = [b for b in banks if too_old(b, today, args.check_age)]
        if old:
            print("Данные не обновлялись дольше "
                  f"{args.check_age} дней:", file=sys.stderr)
            for b in old:
                print(f"  – {b['name']} ({b['id']}): {b.get('checked_at')}, "
                      f"статус {b.get('status')}", file=sys.stderr)
            return 1
        print(f"У всех банков данные свежее {args.check_age} дней")
        return 0

    if args.ids:
        chosen = check_ids(banks, parse_ids(args.ids))
    elif args.all:
        chosen = [b["id"] for b in banks]
    else:
        chosen = [b["id"] for b in banks if needs_refresh(b, today)]
    print(",".join(chosen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
