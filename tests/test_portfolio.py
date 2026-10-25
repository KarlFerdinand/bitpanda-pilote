from datetime import date

from bot.portfolio import analyse, classify

from conftest import EUR_ID, FakeClient


def test_classify():
    assert classify({"type": "CRYPTO", "name": "Bitcoin"}) == "Crypto"
    assert classify({"type": "SECURITY", "name": "iShares Core MSCI World UCITS ETF", "isin": "IE00B4L5Y983"}) == "ETF / ETC"
    assert classify({"type": "SECURITY", "name": "Apple Inc.", "isin": "US0378331005"}) == "Actions"
    assert classify({"type": "METAL", "name": "Gold"}) == "Métaux"


def test_analyse_writes_factual_report(cfg, tmp_path):
    prices = {a["key"]: 100.0 for a in cfg["universe"]}
    client = FakeClient(cfg, prices)
    client.cash = 400.0
    client.extra_rows = [
        {"asset_id": "id-MSCI_WORLD", "balance": {"value": "3"}, "currency_balance": {"value": "300"},
         "invested_amount": {"value": "250"}, "total_return_percent": "20"},
        {"asset_id": "id-BTC", "balance": {"value": "0.002"}, "currency_balance": {"value": "500"},
         "invested_amount": {"value": "300"}, "total_return_percent": "66.7"},
        {"asset_id": "id-NVDA", "balance": {"value": "1"}, "currency_balance": {"value": "200"},
         "invested_amount": {"value": "320"}, "total_return_percent": "-37.5"},
    ]
    assert analyse(cfg, {"BITPANDA_API_KEY": "k"}, client=client, journal_base=tmp_path, today=date(2026, 10, 25)) == 0
    text = (tmp_path / "PORTEFEUILLE.md").read_text()
    assert "1\u202f000,00 €" in text and "Liquidités EUR | 400,00 €" in text
    assert "| Crypto | 500,00 € | 50,0% | 20,0% |" in text
    assert "au-dessus du plafond" in text and "NVIDIA représente 20,0%" in text and "-37,5%" in text
    assert not client.accepted  # lecture seule


def test_analyse_without_key(cfg, tmp_path):
    assert analyse(cfg, {}, journal_base=tmp_path) == 1
