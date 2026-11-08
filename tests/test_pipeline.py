import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from schema import Bank, Dataset, Promo
from selection import needs_refresh, parse_ids, too_old
from validate import clean_bank, has_substance, norm, quote_ok, valid_rate
from build import pct, plural, ru_date, annotate


class TestSchema(unittest.TestCase):
    def test_promo_qr_detection(self):
        p_qr = Promo(title="Прием оплаты по СБП и QR коду", rate=0.4)
        self.assertTrue(p_qr.is_qr_payment)

        p_card = Promo(title="Специальное предложение по эквайрингу", rate=1.0)
        self.assertFalse(p_card.is_qr_payment)

    def test_best_rate_excludes_qr(self):
        b = Bank(
            id="test",
            name="Test Bank",
            color="#000",
            source_url="https://example.com",
            checked_at=date.today(),
            base_rate_from=1.2,
            promos=[
                Promo(title="Оплата через QR", rate=0.2),  # Should be ignored for best_rate
                Promo(title="Спецтариф для новых клиентов", rate=1.0),
            ],
        )
        self.assertEqual(b.best_rate, 1.0)

    def test_best_rate_fallback_to_base(self):
        b = Bank(
            id="test",
            name="Test Bank",
            color="#000",
            source_url="https://example.com",
            checked_at=date.today(),
            base_rate_from=1.5,
            promos=[],
        )
        self.assertEqual(b.best_rate, 1.5)


class TestSelection(unittest.TestCase):
    def test_parse_ids(self):
        self.assertEqual(parse_ids("sber,vtb"), ["sber", "vtb"])
        self.assertEqual(parse_ids("sber vtb tbank"), ["sber", "vtb", "tbank"])
        self.assertEqual(parse_ids(""), [])
        self.assertEqual(parse_ids(None), [])

    def test_too_old(self):
        today = date(2026, 11, 8)
        fresh_bank = {"checked_at": "2026-11-07"}
        stale_bank = {"checked_at": "2026-10-30"}
        self.assertFalse(too_old(fresh_bank, today, 4))
        self.assertTrue(too_old(stale_bank, today, 4))

    def test_needs_refresh(self):
        today = date(2026, 11, 8)
        b_ok = {"status": "ok", "checked_at": "2026-11-07"}
        b_stale = {"status": "stale", "checked_at": "2026-11-07"}
        b_old = {"status": "ok", "checked_at": "2026-10-25"}
        self.assertFalse(needs_refresh(b_ok, today))
        self.assertTrue(needs_refresh(b_stale, today))
        self.assertTrue(needs_refresh(b_old, today))


class TestValidate(unittest.TestCase):
    def test_valid_rate(self):
        self.assertTrue(valid_rate(0.0))
        self.assertTrue(valid_rate(0.3))
        self.assertTrue(valid_rate(2.5))
        self.assertTrue(valid_rate(5.0))
        self.assertTrue(valid_rate(None))
        self.assertFalse(valid_rate(-0.1))
        self.assertFalse(valid_rate(5.5))
        self.assertFalse(valid_rate("1.0"))

    def test_norm_and_quote_ok(self):
        hay = "Комиссия за торговый эквайринг от 0,9% при покупке оборудования."
        norm_hay = norm(hay)
        self.assertTrue(quote_ok("эквайринг от 0,9%", norm_hay))
        self.assertFalse(quote_ok("ставка 0,2%", norm_hay))
        self.assertFalse(quote_ok("коротко", norm_hay))  # < 10 chars

    def test_has_substance(self):
        self.assertTrue(has_substance({"base_rate_from": 1.0}))
        self.assertTrue(has_substance({"promos": [{"title": "Акция"}]}))
        self.assertTrue(has_substance({"base_rate_note": "Индивидуальный тариф"}))
        self.assertTrue(has_substance({"extras": ["Бесплатная доставка"]}))
        self.assertFalse(has_substance({}))
        self.assertFalse(has_substance({"base_rate_note": "   "}))


class TestBuild(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(pct(0.3), "0,3%")
        self.assertEqual(pct(1.0), "1%")
        self.assertEqual(pct(0.0), "0%")
        self.assertEqual(pct(None), "—")

        self.assertEqual(ru_date(date(2026, 11, 8)), "08.11.2026")
        self.assertEqual(ru_date(None), "—")

        self.assertEqual(plural(1, "банк", "банка", "банков"), "банк")
        self.assertEqual(plural(2, "банк", "банка", "банков"), "банка")
        self.assertEqual(plural(5, "банк", "банка", "банков"), "банков")
        self.assertEqual(plural(6, "банк", "банка", "банков"), "банков")
        self.assertEqual(plural(21, "банк", "банка", "банков"), "банк")


if __name__ == "__main__":
    unittest.main()
