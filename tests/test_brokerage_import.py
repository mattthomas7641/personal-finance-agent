import os
import tempfile
from datetime import date

os.environ["LEDGER_DATA"] = os.environ.get("LEDGER_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.db import init_db, session  # noqa: E402
from app.ingest.brokerage import guess_kind, import_fidelity_positions, parse_fidelity_positions  # noqa: E402
from app.models import RetirementAccount  # noqa: E402

init_db()

# Shape of Fidelity's "Portfolio_Positions" download: BOM, holdings rows, blank line, disclaimer, download date.
FIDELITY = (
    "﻿Account Number,Account Name,Symbol,Description,Quantity,Last Price,Last Price Change,Current Value,"
    "Today's Gain/Loss Dollar,Today's Gain/Loss Percent,Total Gain/Loss Dollar,Total Gain/Loss Percent,"
    "Percent Of Account,Cost Basis Total,Average Cost Basis,Type\n"
    'Z12345678,ROTH IRA,FXAIX,FIDELITY 500 INDEX FUND,50.123,$212.40,+$1.10,"$10,646.13",+$55.13,+0.52%,"+$1,646.13",+18.29%,73.42%,"$9,000.00",$179.56,Cash,\n'
    "Z12345678,ROTH IRA,SPAXX**,HELD IN MONEY MARKET,,,,$3853.87,,,,,26.58%,,,Cash,\n"
    'X98765432,INDIVIDUAL,VTI,VANGUARD TOTAL STOCK MARKET ETF,20,$290.00,-$0.50,"$5,800.00",-$10.00,-0.17%,+$800.00,+16.00%,96.67%,"$5,000.00",$250.00,Cash,\n'
    "X98765432,INDIVIDUAL,Pending Activity,,,,,$200.00,,,,,,,,,\n"
    '81234,ACME CORP 401(K) PLAN,1234,FID FREEDOM 2060,1000.5,$38.00,--,"$38,019.00",--,--,--,--,100%,--,--,,\n'
    "\n"
    '"The data and information in this spreadsheet is provided to you solely for your use and is not for distribution."\n'
    '"Date downloaded Oct-14-2026 10:31 a.m ET"\n'
)


@pytest.fixture(autouse=True)
def clean():
    with session() as s:
        for a in s.exec(select(RetirementAccount)).all():
            s.delete(a)
        s.commit()


def test_parse_sums_holdings_per_account():
    accts = {a["number_last4"]: a for a in parse_fidelity_positions(FIDELITY)}
    assert accts["5678"]["value"] == 14500.00 and accts["5678"]["kind"] == "roth_ira" and accts["5678"]["holdings"] == 2
    assert accts["5432"]["value"] == 6000.00 and accts["5432"]["kind"] == "brokerage"  # includes pending activity
    assert accts["1234"]["value"] == 38019.00 and accts["1234"]["kind"] == "401k"


def test_older_combined_name_number_format():
    old = ("Account Name/Number,Symbol,Description,Quantity,Last Price,Current Value\n"
           'ROTH IRA - 223344556,FXAIX,500 INDEX,10,$200.00,"$2,000.00"\n'
           'ROTH IRA - 223344556,SPAXX**,MONEY MARKET,,,$50.00\n')
    [a] = parse_fidelity_positions(old)
    assert a["name"] == "ROTH IRA" and a["number_last4"] == "4556" and a["value"] == 2050.0


@pytest.mark.parametrize("name,kind", [
    ("ACME CORP 401(K) PLAN", "401k"), ("THE 403(B) PLAN", "401k"), ("ROTH IRA", "roth_ira"),
    ("ROLLOVER IRA", "traditional_ira"), ("Health Savings Account", "hsa"), ("INDIVIDUAL - TOD", "brokerage"),
    ("Cash Management (Individual)", "brokerage"), ("529 COLLEGE", "other"),
])
def test_guess_kind(name, kind):
    assert guess_kind(name) == kind


def test_reimport_updates_instead_of_duplicating():
    first = import_fidelity_positions(FIDELITY)
    assert len(first["created"]) == 3 and first["as_of"] == "2026-10-14" and first["total"] == 58519.00
    with session() as s:
        roth = s.exec(select(RetirementAccount).where(RetirementAccount.external_id == "fidelity:5678")).one()
        assert roth.name == "Fidelity Roth Ira ••5678" and roth.balance == 14500
        roth.name = "My Roth"  # user renames it; a re-import must keep the name
        s.add(roth)
        s.commit()
    second = import_fidelity_positions(FIDELITY.replace('"$10,646.13"', '"$11,646.13"'))
    assert second["created"] == [] and len(second["updated"]) == 3
    with session() as s:
        rows = s.exec(select(RetirementAccount)).all()
        roth = [r for r in rows if r.external_id == "fidelity:5678"][0]
    assert len(rows) == 3 and roth.name == "My Roth" and roth.balance == 15500 and roth.as_of == date(2026, 10, 14)


def test_only_last4_of_account_number_stored():
    import_fidelity_positions(FIDELITY)
    with session() as s:
        stored = " ".join(f"{a.name} {a.external_id}" for a in s.exec(select(RetirementAccount)).all())
    assert "12345678" not in stored and "98765432" not in stored


def test_rejects_wrong_file():
    with pytest.raises(ValueError, match="Fidelity positions"):
        parse_fidelity_positions("Date,Description,Amount\n2026-10-01,COFFEE,-4.50\n")


def test_upload_route():
    from app.main import app
    with TestClient(app, client=("127.0.0.1", 5000), follow_redirects=False) as c:
        r = c.post("/retirement/import-fidelity", files={"file": ("Portfolio_Positions.csv", FIDELITY, "text/csv")})
        assert r.status_code == 303 and "imported=" in r.headers["location"]
        r = c.post("/retirement/import-fidelity", files={"file": ("bank.csv", "a,b\n1,2\n", "text/csv")})
        assert "error=" in r.headers["location"]
        assert c.get("/retirement").status_code == 200
