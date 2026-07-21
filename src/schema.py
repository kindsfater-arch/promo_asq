"""Модель данных по акциям эквайринга и СБП.

Единственный источник правды о форме данных: этой схемой пользуются
extract.py (как response_schema для Gemini), validate.py и build.py.

Совместимо с Python 3.9 (системный питон на маке) и 3.12 (раннер Actions).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, Field

# Ставка эквайринга физически не может быть отрицательной или заоблачной.
# Всё, что вне диапазона, — почти наверняка выдернутое не то число
# (год, сумма кешбэка, номер тарифа). Используется в validate.py.
RATE_MIN = 0.0
RATE_MAX = 5.0

STATUSES = ("seed", "ok", "stale", "failed")


class Promo(BaseModel):
    """Временная акция или спецпредложение."""

    title: str
    rate: Optional[float] = Field(
        None, description="Ставка по акции в процентах, если акция про ставку"
    )
    rate_note: Optional[str] = Field(
        None, description="Человеческая формулировка: '0% при обороте до 500 000 ₽/мес'"
    )
    valid_from: Optional[date] = None
    valid_until: Optional[date] = None
    audience: Optional[str] = Field(
        None, description="Кому адресована: 'новым клиентам', 'МСБ'"
    )
    conditions: List[str] = Field(default_factory=list)
    source_quote: Optional[str] = Field(
        None, description="Дословная цитата со страницы банка, откуда взяты условия"
    )
    ended: bool = Field(
        False, description="Акция уже завершилась — показывать только в примечаниях"
    )


class SbpRate(BaseModel):
    """Ставка СБП для конкретного сегмента бизнеса."""

    segment: str = Field(description="'ЖКХ', 'товары повседневного спроса', 'остальные'")
    rate: Optional[float] = None
    note: Optional[str] = None
    source_quote: Optional[str] = None


class Bank(BaseModel):
    id: str
    name: str
    color: str = Field(description="Фирменный цвет, hex")
    source_url: str
    checked_at: date
    status: str = Field("seed", description="seed | ok | stale | failed")

    base_rate_from: Optional[float] = Field(
        None, description="Минимальная базовая ставка торгового эквайринга, %"
    )
    base_rate_note: Optional[str] = None
    base_rate_quote: Optional[str] = None

    promos: List[Promo] = Field(default_factory=list)

    sbp_rates: List[SbpRate] = Field(default_factory=list)
    sbp_note: Optional[str] = None

    vat_note: Optional[str] = Field(
        None, description="Как обстоит дело с НДС на комиссию"
    )
    sbp_vat_free: Optional[bool] = Field(
        None,
        description=(
            "true — банк прямо заявляет, что НДС на комиссию по СБП не начисляется; "
            "false — начисляется; null — на странице не раскрыто. "
            "Отдельное поле, а не разбор vat_note: формулировки у банков разные."
        ),
    )
    extras: List[str] = Field(
        default_factory=list, description="Доп. сервисы и неценовые плюшки"
    )
    notes: List[str] = Field(
        default_factory=list,
        description="Расхождения и всё, что требует ручной проверки",
    )

    @property
    def active_promos(self) -> List[Promo]:
        today = date.today()
        return [
            p
            for p in self.promos
            if not p.ended and (p.valid_until is None or p.valid_until >= today)
        ]

    @property
    def ended_promos(self) -> List[Promo]:
        today = date.today()
        return [
            p
            for p in self.promos
            if p.ended or (p.valid_until is not None and p.valid_until < today)
        ]

    @property
    def best_rate(self) -> Optional[float]:
        """Лучшая ставка, которую клиент реально может получить сегодня."""
        candidates = [p.rate for p in self.active_promos if p.rate is not None]
        if self.base_rate_from is not None:
            candidates.append(self.base_rate_from)
        return min(candidates) if candidates else None

    @property
    def min_sbp_rate(self) -> Optional[float]:
        rates = [s.rate for s in self.sbp_rates if s.rate is not None]
        return min(rates) if rates else None

    @property
    def max_sbp_rate(self) -> Optional[float]:
        rates = [s.rate for s in self.sbp_rates if s.rate is not None]
        return max(rates) if rates else None


class Dataset(BaseModel):
    generated_at: datetime
    banks: List[Bank]

    def by_id(self, bank_id: str) -> Optional[Bank]:
        for b in self.banks:
            if b.id == bank_id:
                return b
        return None
