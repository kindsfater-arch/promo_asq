"""Модель данных по спецпредложениям банков в торговом эквайринге.

Единственный источник правды о форме данных: этой схемой пользуются
extract.py (как response_schema для Gemini), validate.py и build.py.

Ставки СБП сознательно не собираются: на отслеживаемых страницах их нет,
они живут на отдельных страницах банков. Обещать в интерфейсе то, чего
нет в источнике, хуже, чем не показывать вовсе.

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

# Ставка по QR-оплате несопоставима со ставкой эквайринга — она кратно
# ниже. Акции с этими словами не участвуют в расчёте лучшей ставки.
QR_MARKERS = ("qr", "куар", "сбп", "быстрых платеж")


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

    @property
    def is_qr_payment(self) -> bool:
        """Акция целиком про оплату по QR, а не про эквайринг.

        Смотрим только название. Условия трогать нельзя: у Сбера в акции
        «Сниженная комиссия для новых клиентов» SberPay QR упомянут как
        один из каналов, но сама акция — эквайринговая, со ставкой 1%.
        """
        return any(m in self.title.lower() for m in QR_MARKERS)


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

    vat_note: Optional[str] = Field(
        None, description="Как обстоит дело с НДС на комиссию"
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
        """Лучшая ставка эквайринга, доступная клиенту сегодня.

        Акции про оплату по QR и СБП сюда не входят. Иначе получается
        подлог: у БСПБ предложение «приём по QR — комиссия от 0%»
        выдавало best_rate = 0%, и карточка обещала бесплатный
        эквайринг при реальной базовой ставке 0,9%.
        """
        candidates = [
            p.rate for p in self.active_promos
            if p.rate is not None and not p.is_qr_payment
        ]
        if self.base_rate_from is not None:
            candidates.append(self.base_rate_from)
        return min(candidates) if candidates else None


class Dataset(BaseModel):
    generated_at: datetime
    banks: List[Bank]

    def by_id(self, bank_id: str) -> Optional[Bank]:
        for b in self.banks:
            if b.id == bank_id:
                return b
        return None
