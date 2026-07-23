#!/usr/bin/env python3
"""Приёмка извлечённых данных перед публикацией.

Сайт обновляется молча, без ревью и уведомлений, поэтому единственная
преграда между галлюцинацией модели и живой страницей — этот файл.

Логика: каждое значение проверяется отдельно; не прошедшее выбрасывается,
а не правится. Если у банка после отбраковки не осталось содержания —
он сохраняет прошлые данные и помечается stale. Если развалилось больше
MAX_FAILED банков, скрипт падает и публикация не происходит вовсе.

    python src/validate.py                    # применить к data/banks.json
    python src/validate.py --check-only       # только отчёт
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from schema import RATE_MAX, RATE_MIN, Bank, Dataset  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "banks.json"
SNAP_DIR = ROOT / "data" / "snapshots"

# Больше двух развалившихся банков из шести — это не разовый сбой сайта,
# а сломанный пайплайн. Тогда лучше не публиковать ничего.
MAX_FAILED = 2

# Акция, якобы действующая до 2035 года, — почти всегда неверно распознанная дата.
MAX_FUTURE = timedelta(days=730)

MIN_QUOTE = 10


def norm(s: str) -> str:
    """Для сверки цитаты со снимком: регистр, пробелы и дефисы не считаем."""
    s = s.lower().replace(" ", " ").replace("‑", "-").replace("—", "-")
    s = s.replace("–", "-").replace("«", '"').replace("»", '"')
    return re.sub(r"\s+", " ", s).strip()


class Report:
    def __init__(self) -> None:
        self.dropped: List[str] = []
        self.failed: List[str] = []

    def drop(self, bank: str, what: str, why: str) -> None:
        self.dropped.append(f"{bank}: {what} — {why}")


def valid_rate(value: Optional[float]) -> bool:
    return value is None or (
        isinstance(value, (int, float)) and RATE_MIN <= value <= RATE_MAX
    )


def parse_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def quote_ok(quote: Optional[str], haystack: str) -> bool:
    """Цитата должна реально встречаться в снимке — защита от выдумки."""
    if not quote:
        return True
    if len(quote.strip()) < MIN_QUOTE:
        return False
    return norm(quote) in haystack


def clean_bank(bank_id: str, data: Dict, snapshot: str, today: date,
               rep: Report) -> Dict:
    """Выбрасывает всё, что не прошло проверку. Возвращает остаток."""
    hay = norm(snapshot)
    out = dict(data)

    if not valid_rate(out.get("base_rate_from")):
        rep.drop(bank_id, f"базовая ставка {out['base_rate_from']}",
                 f"вне диапазона {RATE_MIN}–{RATE_MAX}%")
        out["base_rate_from"] = None
    if not quote_ok(out.get("base_rate_quote"), hay):
        rep.drop(bank_id, "цитата к базовой ставке", "не найдена в снимке")
        out["base_rate_quote"] = None

    promos = []
    for p in out.get("promos") or []:
        title = (p.get("title") or "").strip()
        if not title:
            rep.drop(bank_id, "акция без названия", "пропущена")
            continue
        if not valid_rate(p.get("rate")):
            rep.drop(bank_id, f"ставка {p.get('rate')} в акции «{title}»",
                     "вне допустимого диапазона")
            p["rate"] = None
        until = parse_date(p.get("valid_until"))
        if until and until > today + MAX_FUTURE:
            rep.drop(bank_id, f"дата {until} в акции «{title}»",
                     "слишком далеко в будущем")
            p["valid_until"] = None
        if not quote_ok(p.get("source_quote"), hay):
            rep.drop(bank_id, f"цитата в акции «{title}»", "не найдена в снимке")
            p["source_quote"] = None
        promos.append(p)
    out["promos"] = promos

    return out


def has_substance(data: Dict) -> bool:
    """Есть ли в извлечении осмысленное содержание.

    Не требуем именно числовую ставку: ГПБ публикует только индивидуальные
    тарифы, и по прежнему критерию (ставка ИЛИ акции) он оставался вечно
    stale, хотя страница прочитана верно. Считаем содержанием и текстовое
    описание тарифа — оно означает, что модель реально разобрала страницу,
    а не вернула пустышку после отказа.
    """
    return bool(
        data.get("base_rate_from") is not None
        or data.get("promos")
        or (data.get("base_rate_note") or "").strip()
    )


def merge(current: Dataset, extracted: Dict[str, Dict], snap_dir: Path,
          today: date, rep: Report) -> Tuple[List[Dict], int]:
    """Накладывает проверенные данные на текущие. Возвращает (банки, ok)."""
    banks_out, ok = [], 0

    for bank in current.banks:
        prev = bank.model_dump(mode="json")
        raw = extracted.get(bank.id)

        if raw is None:
            rep.failed.append(bank.id)
            prev["status"] = "stale"
            banks_out.append(prev)
            continue

        snap_file = snap_dir / f"{bank.id}.txt"
        snapshot = snap_file.read_text(encoding="utf-8") if snap_file.is_file() else ""
        cleaned = clean_bank(bank.id, raw, snapshot, today, rep)

        if not has_substance(cleaned):
            rep.failed.append(bank.id)
            prev["status"] = "stale"
            banks_out.append(prev)
            continue

        # Идентичность банка наша, содержание — из извлечения.
        merged = dict(prev)
        merged.update(cleaned)

        # Предохранитель от «дребезга» модели: страница банка почти всегда
        # содержит рекламную ставку, поэтому пустой base_rate_from в новом
        # прогоне — это промах извлечения, а не отмена ставки банком. Не
        # даём сбойному прогону затереть уже известную ставку (именно так
        # у Сбера 0,3% подменялись на 1% из промо-сноски).
        if (cleaned.get("base_rate_from") is None
                and prev.get("base_rate_from") is not None):
            merged["base_rate_from"] = prev["base_rate_from"]
            merged["base_rate_note"] = prev.get("base_rate_note")
            merged["base_rate_quote"] = prev.get("base_rate_quote")
            rep.drop(bank.id, "базовая ставка",
                     "не извлеклась в этот раз — сохранена прошлая")

        merged["checked_at"] = today.isoformat()
        merged["status"] = "ok"
        banks_out.append(merged)
        ok += 1

    return banks_out, ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshots", type=Path, default=None)
    ap.add_argument("--extracted", type=Path, default=None)
    ap.add_argument("--check-only", action="store_true",
                    help="показать отчёт, не трогая data/banks.json")
    args = ap.parse_args()

    today = date.today()
    snap_dir = args.snapshots or SNAP_DIR / today.isoformat()
    extracted_file = args.extracted or snap_dir / "extracted.json"

    if not extracted_file.is_file():
        print(f"Нет {extracted_file}. Сначала запустите src/extract.py",
              file=sys.stderr)
        return 2

    current = Dataset(**json.loads(DATA_FILE.read_text(encoding="utf-8")))
    extracted = json.loads(extracted_file.read_text(encoding="utf-8"))

    rep = Report()
    banks_out, ok = merge(current, extracted, snap_dir, today, rep)

    if rep.dropped:
        print("Отбраковано:")
        for line in rep.dropped:
            print(f"  – {line}")
    if rep.failed:
        print(f"\nБез свежих данных (оставлены прошлые, помечены stale): "
              f"{', '.join(rep.failed)}")
    print(f"\nОбновлено {ok} из {len(current.banks)} банков")

    if len(rep.failed) > MAX_FAILED:
        print(f"\n✗ Развалилось {len(rep.failed)} банков при пороге {MAX_FAILED}. "
              f"Публикация отменена — данные не записаны.", file=sys.stderr)
        return 1

    if args.check_only:
        print("(--check-only: файл не изменён)")
        return 0

    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "banks": banks_out}
    # Прогон через модель — гарантия, что build.py прочитает записанное.
    Dataset(**payload)
    DATA_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"✓ {DATA_FILE.relative_to(ROOT)} обновлён")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
