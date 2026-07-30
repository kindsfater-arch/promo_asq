#!/usr/bin/env python3
"""Приёмка извлечённых данных перед публикацией.

Сайт обновляется молча, без ревью и уведомлений, поэтому единственная
преграда между галлюцинацией модели и живой страницей — этот файл.

Логика: каждое значение проверяется отдельно; не прошедшее выбрасывается,
а не правится. Если у банка после отбраковки не осталось содержания —
он сохраняет прошлые данные и помечается stale.

Публикация отменяется целиком, только если развалилось больше MAX_FAILED
банков, чьи снимки дошли до модели: значит, сломан пайплайн, а не сайты.
Банк, чью страницу вообще не отдали, к порогу не относится — это про его
защиту от ботов, и лечится дообновлением, а не остановкой публикации.

При точечном прогоне (`--banks`, `--stale`) в расчёт идут только
названные банки: остальные переносятся в новый файл как есть, вместе со
статусом и датой проверки. Без этого дообновление одного ВТБ пометило бы
пять оставшихся банков как stale — они в этом прогоне не собирались.

    python src/validate.py                    # применить к data/banks.json
    python src/validate.py --stale            # принять только дообновлённые
    python src/validate.py --check-only       # только отчёт
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent))
import selection  # noqa: E402
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
        self.failed: List[str] = []      # снимок был, содержания не вышло
        self.unfetched: List[str] = []   # страницу не отдали
        self.skipped: List[str] = []     # банк вне прогона
        self.rescued: List[str] = []     # акция, которую модель потеряла

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


def same_offer(a: Dict, b: Dict) -> bool:
    """Одно и то же предложение под разными формулировками?

    По одному названию судить нельзя: модель переписывает их от прогона к
    прогону («для новых клиентов» вместо «для новых корпоративных
    клиентов»), и предохранитель ниже вернул бы ту же акцию вторым
    экземпляром. Надёжнее цитата — она дословная, и у одной акции варианты
    цитаты вложены друг в друга, — плюс совпадение ставки и срока.
    """
    title_a, title_b = norm(a.get("title") or ""), norm(b.get("title") or "")
    if title_a and title_a == title_b:
        return True

    quote_a, quote_b = norm(a.get("source_quote") or ""), norm(b.get("source_quote") or "")
    if quote_a and quote_b and (quote_a in quote_b or quote_b in quote_a):
        return True

    rate_a, rate_b = a.get("rate"), b.get("rate")
    until_a, until_b = parse_date(a.get("valid_until")), parse_date(b.get("valid_until"))
    return (rate_a is not None and rate_a == rate_b
            and until_a is not None and until_a == until_b)


def rescue_promos(new: List[Dict], old: List[Dict], snapshot: str,
                  bank_id: str, rep: Report) -> List[Dict]:
    """Возвращает акции, которые потеряло извлечение, а страница сохранила.

    Тот же предохранитель, что стоит на базовой ставке, только для акций.
    Когда основная модель отдаёт 503 и работу доделывает запасная, она
    регулярно приносит по странице одну акцию вместо четырёх: 30.07 у
    Сбера так пропали компенсация НДС и два региональных предложения,
    хотя на странице они лежали на прежнем месте.

    Критерий возврата один: дословная цитата потерянной акции всё ещё
    встречается в сегодняшнем снимке. Значит, предложение на странице
    осталось и промахнулось извлечение. Акцию, которую банк действительно
    снял, цитата не спасёт — её в снимке уже нет.
    """
    hay = norm(snapshot)
    out = list(new)
    for promo in old:
        quote = promo.get("source_quote")
        if not promo.get("title") or not quote:
            continue
        if any(same_offer(promo, kept) for kept in out):
            continue
        if not quote_ok(quote, hay):
            continue
        out.append(promo)
        rep.rescued.append(f"{bank_id}: «{promo.get('title')}»")
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
          today: date, rep: Report, scope: Set[str]) -> Tuple[List[Dict], int]:
    """Накладывает проверенные данные на текущие. Возвращает (банки, ok)."""
    banks_out, ok = [], 0

    for bank in current.banks:
        prev = bank.model_dump(mode="json")

        # Банк вне прогона трогать нельзя ничем: ни статусом, ни датой.
        # Он не собирался, значит про него ничего нового не известно.
        if bank.id not in scope:
            rep.skipped.append(bank.id)
            banks_out.append(prev)
            continue

        snap_file = snap_dir / f"{bank.id}.txt"
        raw = extracted.get(bank.id)

        # Страницу не отдали вовсе — это про сайт банка, а не про нас, и
        # к порогу приёмки такой банк не относится (см. main). Данные
        # остаются прошлыми, статус stale, дальше его берёт дообновление.
        if not snap_file.is_file():
            rep.unfetched.append(bank.id)
            prev["status"] = "stale"
            banks_out.append(prev)
            continue

        if raw is None:
            rep.failed.append(bank.id)
            prev["status"] = "stale"
            banks_out.append(prev)
            continue

        snapshot = snap_file.read_text(encoding="utf-8")
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

        merged["promos"] = rescue_promos(
            cleaned.get("promos") or [], prev.get("promos") or [],
            snapshot, bank.id, rep,
        )

        merged["checked_at"] = today.isoformat()
        merged["status"] = "ok"
        banks_out.append(merged)
        ok += 1

    return banks_out, ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    selection.add_args(ap)
    ap.add_argument("--snapshots", type=Path, default=None)
    ap.add_argument("--extracted", type=Path, default=None)
    ap.add_argument("--check-only", action="store_true",
                    help="показать отчёт, не трогая data/banks.json")
    args = ap.parse_args()

    today = date.today()
    snap_dir = args.snapshots or SNAP_DIR / today.isoformat()
    extracted_file = args.extracted or snap_dir / "extracted.json"

    stored = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    current = Dataset(**stored)
    scope = {b["id"] for b in selection.select(stored["banks"], args, today)}
    partial = scope != {b.id for b in current.banks}

    if not scope:
        print("Принимать нечего: у всех банков свежие данные")
        return 0

    if not extracted_file.is_file():
        print(f"Нет {extracted_file}. Сначала запустите src/extract.py",
              file=sys.stderr)
        return 2

    extracted = json.loads(extracted_file.read_text(encoding="utf-8"))

    rep = Report()
    banks_out, ok = merge(current, extracted, snap_dir, today, rep, scope)

    if rep.dropped:
        print("Отбраковано:")
        for line in rep.dropped:
            print(f"  – {line}")
    if rep.rescued:
        print("Возвращены акции, которые модель потеряла, а страница сохранила:")
        for line in rep.rescued:
            print(f"  + {line}")
    if rep.unfetched:
        print(f"\nСтраницу не отдали (оставлены прошлые данные, stale): "
              f"{', '.join(rep.unfetched)}")
    if rep.failed:
        print(f"Снимок есть, содержания не вышло (оставлены прошлые, stale): "
              f"{', '.join(rep.failed)}")
    if rep.skipped:
        print(f"Вне прогона (перенесены без изменений): {', '.join(rep.skipped)}")
    print(f"\nОбновлено {ok} из {len(scope)} банков в прогоне")

    # Порог считает только те банки, чей снимок дошёл до модели, но не дал
    # содержания: это признак сломанного пайплайна, и публиковать такое
    # нельзя. Недоступные сайты порогу не подсудны — иначе получается то,
    # что случилось 30.07: три банка не отдали страницы, приёмка сочла
    # прогон развалившимся и отменила публикацию целиком, хотя по трём
    # остальным данные были свежие и верные. Ушедший в stale банк ничего
    # не теряет: его данные остаются прошлыми, а забирает его дообновление.
    if not partial and len(rep.failed) > MAX_FAILED:
        print(f"\n✗ Развалилось {len(rep.failed)} банков при пороге {MAX_FAILED} "
              f"(снимки были, содержания нет). Публикация отменена — данные "
              f"не записаны.", file=sys.stderr)
        return 1

    if args.check_only:
        print("(--check-only: файл не изменён)")
        return 0

    # Дообновление ходит каждые несколько часов и чаще всего ничего не
    # находит. Переписывать файл ради нового generated_at не нужно: пустая
    # правка дошла бы до коммита и засорила историю сайта.
    if banks_out == [b.model_dump(mode="json") for b in current.banks]:
        print("Данные не изменились — data/banks.json не переписан")
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
