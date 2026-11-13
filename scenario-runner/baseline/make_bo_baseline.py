#!/usr/bin/env python3
"""Record what the PYTHON back-office layer answers, so the TypeScript port can be held to it.

Run from the repo root:  python scenario-runner/baseline/make_bo_baseline.py

Covers framework/helpers/bo.py, framework/bo/* and the BO glue in scripts/run_scenario.py. NOTHING
touches the network: `requests.Session.post` is replaced by a recorder that answers from a queue of
canned replies, and any other way out (`HTTPAdapter.send`) raises. `uuid4` and `time.time` are
pinned so trace ids and token-expiry decisions are reproducible.

Writes scenario-runner/baseline/bo.json. Each call is recorded as {"ok": value} or
{"err": "<ExceptionType>", "msg": str(exc)}, plus — for anything that talks HTTP — every request
(url, headers, exact body text, timeout), how many canned replies were left unused, the log records
and the print() lines.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import glob
import hashlib
import io
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import requests  # noqa: E402

import framework.bo.agent_game_settings as ags  # noqa: E402
import framework.bo.bo_api_client as api  # noqa: E402
import framework.bo.denominations as den  # noqa: E402
import framework.bo  # noqa: E402,F401
# framework.bo re-exports the FUNCTION update_game_paytable, which shadows the submodule of the same
# name for `import ... as`; take the module from sys.modules instead.
ugp = sys.modules["framework.bo.update_game_paytable"]
import framework.helpers.bo as hb  # noqa: E402
import run_scenario as rs  # noqa: E402

NOW = 1_800_000_000

# ── offline plumbing ─────────────────────────────────────────────────────────────────


class NoCannedResponse(BaseException):
    """A flow asked for more replies than its case provides -- a bug in THIS script, so it is a
    BaseException: no `except Exception` in the code under test may swallow it."""


HTTP = {"queue": [], "requests": []}
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error", 502: "Bad Gateway"}


def _fake_post(self, url, headers=None, json=None, timeout=None, **kw):  # noqa: A002 - requests' name
    prepared = requests.Request("POST", url, headers=headers, json=json).prepare()
    HTTP["requests"].append({
        "url": url,
        "headers": dict(headers or {}),
        "body": prepared.body.decode("utf-8"),
        "timeout": timeout,
    })
    if not HTTP["queue"]:
        raise NoCannedResponse(url)
    status, text = HTTP["queue"].pop(0)
    r = requests.Response()
    r.status_code = status
    r._content = text.encode("utf-8")
    r.reason = REASONS[status]
    r.url = url
    r.encoding = "utf-8"
    return r


def _no_network(*a, **k):
    raise RuntimeError("network disabled in make_bo_baseline.py")


requests.Session.post = _fake_post
requests.adapters.HTTPAdapter.send = _no_network

CLOCK = [NOW]
_fake_time = types.SimpleNamespace(time=lambda: CLOCK[0])
api.time = _fake_time
hb.time = _fake_time

TRACE = [0]


def _fake_uuid4():
    TRACE[0] += 1
    return f"00000000-0000-4000-8000-{TRACE[0]:012d}"


api.uuid = types.SimpleNamespace(uuid4=_fake_uuid4)

LOGS: list = []


class _Capture(logging.Handler):
    def emit(self, record):
        LOGS.append([record.levelname, record.name, record.getMessage()])


_fw = logging.getLogger("framework")
_fw.setLevel(logging.DEBUG)
_fw.addHandler(_Capture())
_fw.propagate = False

BO_ENV_KEYS = ["BO_TOKEN", "BO_REFRESH_TOKEN", "BO_API_BASE", "BO_GAME_ID", "BO_SAS_ENTITY_ID",
               "BO_AGENT_ID", "BO_CURRENCY_ID", "BO_GAME_CODE", "BO_REFERENCE_BET", "BO_PAYTABLE_CODE"]


def reset(now=NOW, responses=()):
    HTTP["queue"] = [list(r) for r in responses]
    HTTP["requests"] = []
    LOGS.clear()
    TRACE[0] = 0
    CLOCK[0] = now


# ── serialisation ────────────────────────────────────────────────────────────────────


def plain(v):
    if isinstance(v, float) and not math.isfinite(v):
        return {"__float__": repr(v)}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    return v


def outcome(fn, *args, **kwargs):
    try:
        return {"ok": plain(fn(*args, **kwargs))}
    except NoCannedResponse:
        raise
    except Exception as e:  # noqa: BLE001 -- the exception is the recorded answer
        return {"err": type(e).__name__, "msg": str(e)}


async def aoutcome(coro_fn):
    try:
        return {"ok": plain(await coro_fn())}
    except NoCannedResponse:
        raise
    except Exception as e:  # noqa: BLE001
        return {"err": type(e).__name__, "msg": str(e)}


# ── tokens ───────────────────────────────────────────────────────────────────────────


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def jwt(exp, extra=None) -> str:
    payload = {"sub": "qa", "exp": exp}
    payload.update(extra or {})
    return "eyJhbGciOiJIUzI1NiJ9." + b64url(json.dumps(payload, separators=(",", ":")).encode()) + ".sig"


T_VALID = jwt(NOW + 1200)
T_OLD = jwt(NOW + 600)
T_SOON = jwt(NOW + 60)
T_DEAD = jwt(NOW - 300)
T_NEW = jwt(NOW + 1500)


def jwt_cases():
    raw = lambda s: "h." + s + ".s"  # noqa: E731
    cases = {
        "valid": T_VALID,
        "float-exp": jwt(NOW + 0.5),
        "string-exp": jwt("1800000000"),
        "string-exp-spaces": jwt(" 12 "),
        "string-exp-junk": jwt("soon"),
        "null-exp": jwt(None),
        "bool-exp": jwt(True),
        "list-exp": jwt([1]),
        "no-exp": "h." + b64url(b'{"sub":"x"}') + ".s",
        "payload-list": raw(b64url(b'[1,2]')),
        "payload-number": raw(b64url(b'42')),
        "payload-not-json": raw(b64url(b'not json')),
        "two-parts": "a." + b64url(b'{"exp":1}'),
        "four-parts": "a." + b64url(b'{"exp":1}') + ".b.c",
        "empty": "",
        "none": None,
        "len-mod-1": raw("eyJleHAiOjE3MDAwMDAwMDB9x"),
        "already-padded": raw(base64.urlsafe_b64encode(b'{"exp":1700000000}').decode()),
        "std-alphabet": raw(base64.b64encode(b'{"exp":1700000000,"k":"\xfb\xff"}'.decode("latin-1").encode("utf-8")).decode()),
        "urlsafe-alphabet": raw(b64url('{"exp":1700000000,"k":"ûÿ?>"}'.encode("utf-8"))),
        "junk-chars-skipped": raw("eyJle!HAiOjE3MDA*wMDAwMDB9"),
        "pad-in-middle": raw("eyJleHAiOjE3MDAwMDAwMDB9=junk"),
        "pad-early": raw("eyJl=eHAiOjE3MDAwMDAwMDB9"),
        "non-ascii": raw("eyJleHAiOjE3MDAwMDAwMDB9é"),
        "utf8-bom": raw(b64url(b'\xef\xbb\xbf{"exp":1700000001}')),
        "utf16-le": raw(b64url('{"exp":1700000002}'.encode("utf-16-le"))),
        "utf16-be-bom": raw(b64url('{"exp":1700000003}'.encode("utf-16"))),
        "utf32-le": raw(b64url('{"exp":1700000004}'.encode("utf-32-le"))),
        "invalid-utf8": raw(b64url(b'{"exp":1700000000,"k":"\xff"}')),
        "nan-exp": raw(b64url(b'{"exp":NaN}')),
        "infinity-exp": raw(b64url(b'{"exp":Infinity}')),
        "neg-exp": jwt(-5),
        "exp-key-twice": raw(b64url(b'{"exp":1,"exp":2}')),
        "nested-exp": jwt({"v": 1}),
    }
    return [{"name": k, "token": v, "result": outcome(hb.BoHelper._jwt_expiry, v),
             "client_result": outcome(lambda t=v: api.BoApiClient(token=t, refresh_token="r")._token_expiry())}
            for k, v in cases.items()]


# ── canned BO data ───────────────────────────────────────────────────────────────────

CODE = "SL_33_ZAPPIN_ZEUS"


def OK(data=None, **extra):
    body = {"status": "success", "message": "OK", "messageCode": "SUCCESS", "data": data, "traceId": "srv"}
    body.update(extra)
    return body


EXPIRED = {"status": "error", "message": "Token expired", "messageCode": "TOKEN_EXPIRED", "data": None}


def reply(body, status=200):
    return [status, body if isinstance(body, str) else json.dumps(body)]


GAME = {
    "id": "63", "code": CODE, "name": "Zappin Zeus", "description": "Thần Zeus ⚡",
    "gameCategory": {"id": 3, "name": "Slot"}, "gameSubCategory": {"id": 1, "name": "3x3"},
    "maxPayout": 300000, "assets": {"thumbnailSquare": "sq.png", "thumbnailLandscape": "ls.png"},
    "feature": ["FS"], "theme": ["Greek"], "labels": [], "tags": ["hot"], "languages": ["en", "vi"],
    "hitrate": 0.35, "status": 1, "rtp": "96", "winFormula": "a*b+c",
    "currencyIds": [18, 31], "currency": "x", "rtpRange": "94-96", "gameId": 63, "currencies": [],
    "currencyStatus": 1,
    "currencyInfoList": [
        {"id": 31, "code": "TKN", "denominationId": 7, "denomName": "TKN 3x3", "minBet": 0.2, "maxBet": 100,
         "isDenomOverride": 0, "isDefault": 1, "extra": "x"},
        {"id": 18, "code": "USD", "denominationId": 3, "denomName": "USD 3x3", "minBet": 0.1, "maxBet": 50,
         "isDenomOverride": 1, "isDefault": 0},
        {"id": 5, "code": "EUR", "denominationId": 4, "denomName": "EUR 3x3", "minBet": 0.1, "maxBet": 50,
         "isDenomOverride": 0, "isDefault": 0},
    ],
    "payTableInfoList": [
        {"code": "DEFAULT", "id": 360, "isDefault": 1, "payTable": "{\"a\":1}", "rtp": "MEDIUM", "status": 1,
         "lastUpdated": "2026-01-01"},
        {"code": "AutomationTest", "id": 362, "isDefault": 0, "payTable": "{\"b\":2}", "rtp": "MEDIUM", "status": 0},
    ],
}


def game_with(**changes):
    g = copy.deepcopy(GAME)
    g.update(changes)
    return g


GAME_NO_AT = game_with(payTableInfoList=[GAME["payTableInfoList"][0]])
GAME_NO_DEFAULT = game_with(payTableInfoList=[
    {"code": "P1", "id": 370, "isDefault": 0, "payTable": "{\"p\":1}", "rtp": "LOW", "status": 1},
    {"code": "P2", "id": 371, "isDefault": 0, "payTable": "{\"p\":2}", "rtp": "LOW", "status": 1}])
GAME_CREATED = game_with(payTableInfoList=GAME_NO_AT["payTableInfoList"] + [
    {"code": "AutomationTest", "id": 999, "isDefault": 0, "payTable": "{}", "rtp": "HIGH", "status": 1}])

ROW = {"id": 900, "gameCode": CODE, "status": 1, "override": 1, "maxPayoutCustom": "250000.0000",
       "payTableId": 360, "denominationId": 7, "rtpLevel": "MEDIUM", "maxMultiplier": "5000.00",
       "isDenomOverride": 0, "isDefaultPaytable": 1,
       "payTableList": [
           {"payTableId": 360, "payTableCode": "DEFAULT", "isDefault": 1, "rtpLevel": "MEDIUM"},
           {"payTableId": 361, "payTableCode": "DEFAULT_HIGH", "isDefault": 1, "rtpLevel": "HIGH"},
           {"payTableId": 362, "payTableCode": "AutomationTest", "isDefault": 0, "rtpLevel": "MEDIUM"}]}
ROW_VIP = dict(ROW, id=901, gameCode=CODE + "_VIP", denominationId=8)


def settings(rows=None, **extra):
    data = {"gameSettings": rows if rows is not None else [ROW_VIP, ROW], "maxPayout": 300000, "total": 2,
            "autoFinalizationStatus": 1, "autoFinalizationDuration": " 0.5 "}
    data.update(extra)
    return data


DLIST = {"currency": "TKN", "gameSubCategory": "3x3", "denomType": "x", "denominations": [
    {"id": 7, "isDefault": 1, "name": "TKN 3x3", "defaultBetLevel": 2, "betLevels": [0.2, 0.4, 1],
     "totalLevel": 3, "minBet": 0.2, "maxBet": 1, "remarks": "", "lastUpdated": "2026"},
    {"id": 9, "isDefault": 0, "name": "TKN high", "defaultBetLevel": 1, "betLevels": [5, 10],
     "totalLevel": 2, "minBet": 5, "maxBet": 10, "remarks": "vip", "lastUpdated": "2026"}]}
AUTO = {"id": 55, "isDefault": 0, "name": "Automation  Test TKN 3x3", "defaultBetLevel": 1,
        "betLevels": [11, 22, 33], "totalLevel": 3, "minBet": 11, "maxBet": 33, "remarks": "", "lastUpdated": "2026"}
DLIST_AUTO = dict(DLIST, denominations=DLIST["denominations"] + [AUTO])
DLIST_CREATED = dict(DLIST, denominations=DLIST["denominations"] + [dict(AUTO, id=77, name="automation test TKN 3x3")])
DUP = {"status": "error", "message": "Name exists", "messageCode": "DENOMINATION_NAME_ALREADY_EXISTS", "data": None}


# ── pure functions ───────────────────────────────────────────────────────────────────

PURE = {
    "ags._num": ags._num, "ags._is_nan": ags._is_nan, "ags.iter_game_rows": ags.iter_game_rows,
    "ags.find_game_row": ags.find_game_row, "ags.find_paytable_by_code": ags.find_paytable_by_code,
    "ags.find_default_paytable": ags.find_default_paytable, "ags.find_current_paytable": ags.find_current_paytable,
    "ags.build_submit_row": ags.build_submit_row, "ags._auto_finalization_from": ags._auto_finalization_from,
    "ugp.find_game_paytable": ugp.find_game_paytable, "ugp.build_update_payload": ugp.build_update_payload,
    "ugp.set_paytable": ugp.set_paytable, "ugp._as_paytable_string": ugp._as_paytable_string,
    "den._norm_name": den._norm_name, "den.find_denomination": den.find_denomination,
    "den.denomination_names": den.denomination_names,
    "den.automation_denomination_name": den.automation_denomination_name,
    "den.to_submit_denomination": den.to_submit_denomination, "den._validated_levels": den._validated_levels,
    "den.list_game_denominations": den.list_game_denominations, "den.game_sub_category_id": den.game_sub_category_id,
    "den.find_game_currency": den.find_game_currency, "den.set_currency_denomination": den.set_currency_denomination,
    "rs.max_win_needs_bet": rs.max_win_needs_bet,
}


def pure_cases():
    at = ROW["payTableList"][2]
    dflt = ROW["payTableList"][0]
    C = []

    def add(fn, *args, **kwargs):
        C.append({"fn": fn, "args": copy.deepcopy(list(args)), "kwargs": copy.deepcopy(kwargs),
                  "result": outcome(PURE[fn], *copy.deepcopy(args), **copy.deepcopy(kwargs))})

    for v in ["300000.0000", 1.5, "1e3", True, "abc", None, "nan", 12, "-0.0", " 7 "]:
        add("ags._num", v)
    for v in ["1", "x", None, [], "nan", "inf", 3, "", {"a": 1}, True]:
        add("ags._is_nan", v)
    for d in [settings(), {"gameSettings": {"a": ROW, "b": ROW_VIP}}, {"gameSettings": None}, {}, {"gameSettings": {}}]:
        add("ags.iter_game_rows", d)
    add("ags.find_game_row", settings(), game_code=CODE)
    add("ags.find_game_row", settings(), game_code=CODE + "_VIP")
    add("ags.find_game_row", settings(), game_setting_id=900)
    add("ags.find_game_row", {"gameSettings": {"x": ROW}}, game_code=CODE)
    add("ags.find_game_row", settings(), game_code="NOPE")
    add("ags.find_game_row", settings(), game_code=None, game_setting_id=None)
    add("ags.find_game_row", {"gameSettings": None}, game_code=CODE)
    add("ags.find_paytable_by_code", ROW, "AutomationTest")
    add("ags.find_paytable_by_code", ROW, "Missing")
    add("ags.find_paytable_by_code", dict(ROW, payTableList=None), "X")
    add("ags.find_default_paytable", ROW)
    add("ags.find_default_paytable", ROW, "HIGH")
    add("ags.find_default_paytable", ROW, "LOW")
    add("ags.find_default_paytable", dict(ROW, payTableList=[at, dict(dflt, isDefault=True)]), "")
    add("ags.find_default_paytable", dict(ROW, payTableList=[at]))
    add("ags.find_default_paytable", dict(ROW, payTableList=None))
    add("ags.find_current_paytable", ROW)
    add("ags.find_current_paytable", dict(ROW, payTableId=444))
    add("ags.find_current_paytable", dict(ROW, payTableId=444, isDefaultPaytable=0))
    add("ags.find_current_paytable", dict(ROW, payTableId=444, isDefaultPaytable=None, payTableList=None))
    add("ags.find_current_paytable", dict(ROW, payTableId=444, isDefaultPaytable=True))
    rows = [
        ROW, dict(ROW, override=0), dict(ROW, override=None), dict(ROW, maxPayoutCustom=""),
        dict(ROW, maxPayoutCustom="abc"), dict(ROW, maxPayoutCustom="nan"), dict(ROW, maxPayoutCustom=12.5),
        dict(ROW, maxPayoutCustom=True), dict(ROW, override="1"), dict(ROW, override=2),
        dict(ROW, maxMultiplier=0), dict(ROW, maxMultiplier="0"), dict(ROW, maxMultiplier=None),
        dict(ROW, maxMultiplier="12.50"), dict(ROW, status=0), dict(ROW, status=None), dict(ROW, status="0"),
        {"id": 1},
    ]
    for r in rows:
        add("ags.build_submit_row", r, at, 300000)
    add("ags.build_submit_row", dict(ROW, override=0), at, None)
    add("ags.build_submit_row", ROW, dflt, 300000)
    add("ags.build_submit_row", ROW, dict(at, isDefault=True), 300000)
    add("ags.build_submit_row", ROW, dict(at, isDefault="1"), 300000)
    add("ags.build_submit_row", ROW, at, 300000, 40.0)
    add("ags.build_submit_row", ROW, at, 300000, max_payout=40.25)
    add("ags.build_submit_row", ROW, at, 300000, max_payout=False)
    add("ags.build_submit_row", ROW, at, None, max_payout=False)
    add("ags.build_submit_row", ROW, at, 300000, max_payout=0)
    add("ags.build_submit_row", ROW, at, 300000, max_payout="1234.5000")
    add("ags.build_submit_row", ROW, at, 300000, denomination_id=55, is_denom_override=1)
    add("ags.build_submit_row", ROW, at, 300000, denomination_id=0, is_denom_override=0)
    add("ags.build_submit_row", ROW, at, 300000, is_denom_override=False)
    add("ags.build_submit_row", ROW, at, 300000, is_denom_override="yes")
    add("ags.build_submit_row", ROW, at, 300000, denomination_id="x")
    for d in [{}, settings(), {"autoFinalizationStatus": "0", "autoFinalizationDuration": 0.75},
              {"autoFinalizationStatus": 0, "autoFinalizationDuration": 0}, {"autoFinalizationDuration": " 1\t"}]:
        add("ags._auto_finalization_from", d)

    add("ugp.find_game_paytable", GAME, "AutomationTest")
    add("ugp.find_game_paytable", GAME, "nope")
    add("ugp.find_game_paytable", {"payTableInfoList": None}, "x")
    for g in [GAME, {"id": 5}, {"code": "x"}, game_with(rtp="96.5"), game_with(rtp=96.5), game_with(rtp=0),
              game_with(rtp=None), game_with(hitrate=0), game_with(hitrate=None), game_with(hitrate="12"),
              game_with(status=True), game_with(status="1"), game_with(status=0), game_with(id=63),
              game_with(gameCategory=None, assets=None, gameSubCategory={}),
              game_with(currencyInfoList=[{"id": 2}, {"id": None}]), game_with(currencyInfoList=[{"id": None}]),
              game_with(currencyInfoList=[{"id": "b"}, {"id": "a"}]), game_with(currencyInfoList=None, payTableInfoList=None),
              game_with(id="6x")]:
        add("ugp.build_update_payload", g)
    payload = ugp.build_update_payload(copy.deepcopy(GAME))
    add("ugp.set_paytable", payload, "AutomationTest", {"symbols": [1, 2.5, "é"], "nested": {"a": None, "b": True}})
    add("ugp.set_paytable", payload, "DEFAULT", "{\"raw\": 1}")
    add("ugp.set_paytable", payload, "Nope", {})
    add("ugp.set_paytable", payload, "AutomationTest", [0.1, -2, 1e-07, 123456789.125])
    add("ugp.set_paytable", {"payTableInfoList": []}, "X", {})
    for v in [{"a": 1}, [1, "x"], "already", None, True, 0.5, {"k": "⚡\U0001F600"}, {"3": 1, "a": {"10": 2, "9": 3}}]:
        add("ugp._as_paytable_string", v)

    for v in ["  Automation   Test TKN 3x3 ", "STRASSE", "straße", None, 12, "A B", "a\x1cb", "", "ΣΑΣ",
              "ﬁne", "İ", "a﻿b", " x "]:
        add("den._norm_name", v)
    add("den.find_denomination", DLIST_AUTO, name="TKN 3x3")
    add("den.find_denomination", DLIST_AUTO, name="automation test tkn 3x3")
    add("den.find_denomination", DLIST_AUTO, denomination_id=9)
    add("den.find_denomination", DLIST_AUTO, name="nope")
    add("den.find_denomination", DLIST_AUTO, denomination_id=1234)
    add("den.find_denomination", DLIST_AUTO)
    add("den.find_denomination", {"denominations": None}, name="x")
    add("den.find_denomination", DLIST_AUTO, name="tkn 3X3", denomination_id=9)
    add("den.denomination_names", DLIST_AUTO)
    add("den.denomination_names", {})
    add("den.automation_denomination_name", DLIST)
    add("den.automation_denomination_name", {})
    add("den.automation_denomination_name", DLIST, "")
    add("den.automation_denomination_name", DLIST, "  qa run ")
    add("den.automation_denomination_name", {"currency": 18, "gameSubCategory": None})
    for e in [AUTO, dict(AUTO, id=None), {"name": "x"}, {}]:
        add("den.to_submit_denomination", e)
    for levels, lvl in [([33, 11, 22], 1), ([], 1), (None, 1), ([11], 2), ([11, 22], "2"), ([11, 22], "x"),
                        ([11, 2.5], 1), (["b", "a"], 1), ([1, "a"], 1), ([11, 22], 0), ([5, 5, 1], 3), ([1, 2], 2.9),
                        ([1, 2], None)]:
        add("den._validated_levels", levels, lvl)
    add("den.list_game_denominations", GAME)
    add("den.list_game_denominations", game_with(currencyInfoList=[{"id": 1, "code": None}, {"id": 2, "code": "AAA"}]))
    add("den.list_game_denominations", {})
    add("den.game_sub_category_id", GAME)
    add("den.game_sub_category_id", game_with(gameSubCategory=None))
    add("den.game_sub_category_id", game_with(gameSubCategory={"id": None}))
    add("den.game_sub_category_id", game_with(gameSubCategory={"id": 0}))
    add("den.find_game_currency", GAME, currency_code="USD")
    add("den.find_game_currency", GAME, currency_id=5)
    add("den.find_game_currency", GAME)
    add("den.find_game_currency", GAME, currency_code="GBP")
    add("den.find_game_currency", game_with(currencyInfoList=[{"code": "B", "id": 2}, {"code": "A", "id": 9},
                                                              {"code": "A", "id": 1}]), currency_id=77)
    add("den.find_game_currency", game_with(currencyInfoList=[{"code": None, "id": 2}, {"code": "A", "id": 9}]),
        currency_id=77)
    add("den.set_currency_denomination", payload, 18, 99)
    add("den.set_currency_denomination", payload, 31, 98, True)
    add("den.set_currency_denomination", payload, 5, 97, False)
    add("den.set_currency_denomination", payload, 5, 96, 0)
    add("den.set_currency_denomination", payload, 1234, 1)
    for pt in [None, "ref", {}, {"max_win_x": 10}, {"max_win_x": 10, "max_payout": 5}, {"max_payout": 5},
               {"max_win_x": None}, {"max_win_x": 0}, ["max_win_x"]]:
        add("rs.max_win_needs_bet", pt)
    return C


# ── max-win resolution ───────────────────────────────────────────────────────────────

def max_payout_cases():
    exprs = [10, 2.5, True, [2], "10 * bet", "max(50, 10 * bet)", "min(100, bet * 3)", "round(bet * 3.333, 1)",
             "bet ** 2", "10 if bet > 1 else 20", "bet // 0.3", "bet % 0.25", "-bet + 100", "(bet + 1) * 2",
             "max([bet, 3])", "round(bet)", "abs(bet)", "len('ab')", "foo * 2", "10 *", "2bet", "'x'", "bet > 1",
             "[bet]", "None", "bet / 0", "1 / 0", "'bet' * 2", "max(bet, abs)", "bet if True else abs(1)", "True",
             "1e3 * bet", "bet * 1_000", "  10 * bet", "max(50)", "round(bet, 1) * 10", "10 * BET", "sum([bet])",
             "bet * 0.1", "100 // 7", "2 ** 0.5 * bet", "bet - bet", "(bet,)", "{'a': bet}", "min(bet)", "bet.real"]
    C = []
    for x in exprs:
        for bet in [0.6, 4.0]:
            C.append({"pt": {"max_win_x": x}, "bet": bet, "reference_bet": None})
    C += [
        {"pt": {"max_payout": 1000}, "bet": None, "reference_bet": None},
        {"pt": {"max_payout": "1500.5"}, "bet": None, "reference_bet": None},
        {"pt": {"max_payout": "abc"}, "bet": None, "reference_bet": None},
        {"pt": {"max_payout": 12.345, "max_win_x": 10}, "bet": 1.0, "reference_bet": None},
        {"pt": {}, "bet": 1.0, "reference_bet": None},
        {"pt": {"max_win_x": 10}, "bet": None, "reference_bet": "0.6"},
        {"pt": {"max_win_x": 10}, "bet": None, "reference_bet": None},
        {"pt": {"max_win_x": 10}, "bet": 0, "reference_bet": "0.6"},
        {"pt": {"max_win_x": 10}, "bet": "0.4", "reference_bet": None},
        {"pt": {"max_win_x": 10}, "bet": True, "reference_bet": None},
        {"pt": {"max_win_x": 10}, "bet": None, "reference_bet": "0"},
        {"pt": {"max_win_x": 10}, "bet": [1], "reference_bet": None},
        {"pt": {"max_win_x": "10 * bet"}, "bet": None, "reference_bet": "abc"},
        {"pt": {"max_win_x": 3}, "bet": 0.1, "reference_bet": None},
        {"pt": {"max_win_x": "bet * 3"}, "bet": 0.1, "reference_bet": None},
        {"pt": {"max_win_x": 1}, "bet": 1.005, "reference_bet": None},
        {"pt": {"max_win_x": 10}, "bet": -2.5, "reference_bet": None},
        {"pt": {"max_win_x": "bet * 2"}, "bet": 1e16, "reference_bet": None},
        {"pt": {"max_win_x": "bet"}, "bet": "inf", "reference_bet": None},
        {"pt": "fixture", "bet": 1.0, "reference_bet": None, "no_bo": True},
    ]
    for c in C:
        bo = None if c.get("no_bo") else types.SimpleNamespace(reference_bet=c["reference_bet"])
        c["result"] = outcome(rs._resolve_max_payout, bo, "zeus", copy.deepcopy(c["pt"]), bet=c["bet"])
    return C


# ── bo_setup / bo_reset / read_base_bet ──────────────────────────────────────────────

class FakeBo:
    def __init__(self, paytable_code=None, reference_bet=None, fail=None):
        self.paytable_code = paytable_code
        self.reference_bet = reference_bet
        self.fail = fail
        self.calls = []

    def resolve_paytable(self, ref=None, inline=None):
        self.calls.append(["resolve_paytable", {"ref": ref, "inline": inline}])
        if inline is not None:
            return inline
        if ref == "missing":
            raise ValueError("No paytable fixture 'missing.json'")
        return {"fixture": ref}

    async def ensure_paytable(self, code, paytable_content=None):
        self.calls.append(["ensure_paytable", {"code": code, "paytable_content": paytable_content}])
        if self.fail == "ensure":
            raise api.ApiError("game/update/v2 failed: boom (X)")
        return 362

    async def select_agent_paytable(self, code, max_payout=None):
        self.calls.append(["select_agent_paytable", {"code": code, "max_payout": max_payout}])
        return {"status": "success"}

    async def update_paytable(self, code, content):
        self.calls.append(["update_paytable", {"code": code, "content": content}])
        return {"status": "success"}

    async def reset_agent_paytable(self):
        self.calls.append(["reset_agent_paytable", {}])
        if self.fail == "reset":
            raise api.AuthError("Unauthorized — re-login required.")
        return {"status": "success"}


def setup_cases():
    specs = [
        {"pt": "fs-trigger9", "bet": None, "bo": {}},
        {"pt": {"ref": "base.json", "code": "Automation_test"}, "bet": None, "bo": {}},
        {"pt": {"ref": "base"}, "bet": None, "bo": {"paytable_code": "Automation_test"}},
        {"pt": {"inline": {"x": 1}, "max_win_x": 10}, "bet": 0.6, "bo": {}},
        {"pt": {"inline": False}, "bet": None, "bo": {}},
        {"pt": {"max_win_x": "max(50, 10 * bet)"}, "bet": 4.0, "bo": {}},
        {"pt": {"max_win_x": 10}, "bet": None, "bo": {"reference_bet": "0.2"}},
        {"pt": {"max_win_x": 10}, "bet": None, "bo": {}},
        {"pt": {"max_payout": 1000}, "bet": None, "bo": {}},
        {"pt": {"max_payout": 12.5, "ref": "x"}, "bet": None, "bo": {}},
        {"pt": {"ref": "missing"}, "bet": None, "bo": {}},
        {"pt": {"code": "", "ref": ""}, "bet": None, "bo": {"paytable_code": ""}},
        {"pt": {"ref": "base"}, "bet": None, "bo": {"fail": "ensure"}},
        {"pt": {"ref": "base"}, "bet": None, "bo": None},
        {"pt": ["ref"], "bet": None, "bo": {}},
        {"pt": {"ref": 5}, "bet": None, "bo": {}},
    ]
    out = []
    for s in specs:
        bo = None if s["bo"] is None else FakeBo(**s["bo"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = asyncio.run(aoutcome(lambda: rs.bo_setup(bo, "zeus", copy.deepcopy(s["pt"]), bet=s["bet"])))
        out.append(dict(s, result=res, calls=plain(bo.calls) if bo else None, printed=buf.getvalue().splitlines()))
    resets = []
    for bo_spec, label in [({}, ""), ({}, "case-1"), ({"fail": "reset"}, "case-2"), ({"fail": "reset"}, ""), (None, "x")]:
        bo = None if bo_spec is None else FakeBo(**bo_spec)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = asyncio.run(aoutcome(lambda: rs.bo_reset(bo, label=label)))
        resets.append({"bo": bo_spec, "label": label, "result": res, "printed": buf.getvalue().splitlines(),
                       "calls": plain(bo.calls) if bo else None})

    class FakePage:
        def __init__(self, value):
            self.value = value
            self.seen = []

        async def evaluate(self, js):
            self.seen.append(js)
            if self.value == "__raise__":
                raise RuntimeError("Execution context was destroyed")
            return self.value

    reads = []
    for v in [4.0, 0.6, "4", 0, -1, None, "abc", True, "__raise__", "nan", [1], 1e-9]:
        page = FakePage(v)
        res = asyncio.run(aoutcome(lambda: rs.read_base_bet(page)))
        reads.append({"value": v, "result": res, "js_is_base_bet": page.seen == [rs._BASE_BET_JS]})
    return {"setup": out, "reset": resets, "read_base_bet": reads,
            "BASE_BET_JS": rs._BASE_BET_JS, "BO_SETTLE_S": rs.BO_SETTLE_S}


# ── HTTP flows through the low-level modules ─────────────────────────────────────────

FLOWS = {
    "post": lambda c, endpoint, body=None: c.post(endpoint, body),
    "ags.get_agent_currencies": ags.get_agent_currencies,
    "ags.get_agent_game_settings": ags.get_agent_game_settings,
    "ags.update_agent_game_settings": ags.update_agent_game_settings,
    "ags.set_agent_paytable": ags.set_agent_paytable,
    "ags.reset_agent_paytable_to_default": ags.reset_agent_paytable_to_default,
    "ags.agent_paytable": None,  # context manager, below
    "ugp.get_game_detail": ugp.get_game_detail,
    "ugp.update_game": ugp.update_game,
    "ugp.update_game_paytable": ugp.update_game_paytable,
    "ugp.ensure_game_paytable": ugp.ensure_game_paytable,
    "den.get_denomination_list": den.get_denomination_list,
    "den.update_denominations": den.update_denominations,
    "den.create_denomination": den.create_denomination,
    "den.update_denomination": den.update_denomination,
    "den.ensure_automation_denomination": den.ensure_automation_denomination,
    "den.assign_game_denomination": den.assign_game_denomination,
    "den.get_agent_denomination": den.get_agent_denomination,
    "den.assign_agent_denomination": den.assign_agent_denomination,
    "den.reset_agent_denomination_to_default": den.reset_agent_denomination_to_default,
    "den.ensure_agent_automation_denomination": den.ensure_agent_automation_denomination,
}


def _agent_paytable(client, *args, raise_in_body=False):
    with ags.agent_paytable(client, *args):
        if raise_in_body:
            raise ValueError("boom")
    return "done"


def flow_cases():
    long_detail = {"status": "error", "message": "bad", "messageCode": "E", "errors": ["x" * 3000]}
    refresh_ok = OK({"token": T_NEW, "refreshToken": None})
    refresh_rot = OK({"token": T_NEW, "refreshToken": "rt-rotated"})
    S = [
        # transport
        {"name": "post-ok", "fn": "post", "args": ["game/view", {"gameId": 63, "sasEntityId": 1}],
         "responses": [OK({"name": "Zeus"})]},
        {"name": "post-none-body", "fn": "post", "args": ["game/view", None], "responses": [OK()]},
        {"name": "post-empty-body", "fn": "post", "args": ["game/view", {}], "responses": [OK()]},
        {"name": "post-own-traceid", "fn": "post", "args": ["x/y", {"traceId": "mine", "a": 1}], "responses": [OK()]},
        {"name": "post-url-strip", "fn": "post", "args": ["/game/view", {"n": "é⚡"}], "responses": [OK()],
         "client": {"api_base": "https://bo.example/api/v1///"}},
        {"name": "post-sc-ok", "fn": "post", "args": ["a", {}], "responses": [{"status": "SC_OK", "data": 1}]},
        {"name": "expired-retry-ok", "fn": "post", "args": ["game/view", {"gameId": 1}], "responses": [EXPIRED, OK(1)]},
        {"name": "expired-refresh", "fn": "post", "args": ["game/view", {"gameId": 1}],
         "responses": [EXPIRED, EXPIRED, refresh_ok, OK(2)]},
        {"name": "expired-refresh-rotated", "fn": "post", "args": ["game/view", {"gameId": 1}],
         "responses": [EXPIRED, EXPIRED, refresh_rot, OK(3)]},
        {"name": "refresh-rejected", "fn": "post", "args": ["game/view", {}],
         "responses": [EXPIRED, EXPIRED, {"status": "error", "message": "Unauthorized", "messageCode": "UNAUTHORIZED_USER"}]},
        {"name": "refresh-no-token", "fn": "post", "args": ["game/view", {}],
         "responses": [EXPIRED, EXPIRED, {"status": "SC_OK", "data": {}}]},
        {"name": "refresh-data-null", "fn": "post", "args": ["game/view", {}],
         "responses": [EXPIRED, EXPIRED, {"status": "success", "data": None}]},
        {"name": "refresh-data-string", "fn": "post", "args": ["game/view", {}],
         "responses": [EXPIRED, EXPIRED, {"status": "success", "data": "x"}]},
        {"name": "no-refresh-token", "fn": "post", "args": ["game/view", {}], "responses": [EXPIRED, EXPIRED],
         "client": {"refresh_token": ""}},
        {"name": "expired-then-unauthorized", "fn": "post", "args": ["a", {}],
         "responses": [EXPIRED, {"status": "error", "messageCode": "UNAUTHORIZED_USER"}]},
        {"name": "unauthorized", "fn": "post", "args": ["a", {}],
         "responses": [{"status": "error", "message": "nope", "messageCode": "UNAUTHORIZED_USER"}]},
        {"name": "api-error-detail", "fn": "post", "args": ["denomination/update", {"x": 1}],
         "responses": [{"status": "error", "message": "One or more validation errors occurred.",
                        "messageCode": "VALIDATION_ERROR", "traceId": "t", "data": None, "empty": "", "zero": 0,
                        "errors": {"name": ["Tên đã tồn tại"]}, "ok": True}]},
        {"name": "api-error-bare", "fn": "post", "args": ["a", {}], "responses": [{"status": "fail", "message": None}]},
        {"name": "api-error-long", "fn": "post", "args": ["a", {}], "responses": [long_detail]},
        {"name": "api-error-no-status", "fn": "post", "args": ["a", {}], "responses": [{"data": 1}]},
        {"name": "http-500", "fn": "post", "args": ["game/view", {}], "responses": [[500, "oops"]]},
        {"name": "http-404", "fn": "post", "args": ["game/view", {}], "responses": [[404, "{}"]]},
        {"name": "non-json", "fn": "post", "args": ["game/view", {}], "responses": [[200, "<html>oops</html>"]]},
        {"name": "reply-list", "fn": "post", "args": ["a", {}], "responses": [[200, "[1, 2]"]]},
        {"name": "nan-body", "fn": "post", "args": ["a", {"x": float("nan")}], "responses": []},
        {"name": "proactive-refresh", "fn": "post", "args": ["game/view", {}], "responses": [refresh_ok, OK(4)],
         "client": {"token": T_SOON}},
        {"name": "proactive-refresh-rejected", "fn": "post", "args": ["game/view", {}],
         "responses": [{"status": "error", "message": "Unauthorized", "messageCode": "UNAUTHORIZED_USER"}],
         "client": {"token": T_DEAD}},
        {"name": "no-proactive", "fn": "post", "args": ["game/view", {}], "responses": [OK(5)],
         "client": {"token": T_OLD}},
        {"name": "opaque-token", "fn": "post", "args": ["game/view", {}], "responses": [OK(6)],
         "client": {"token": "opaque"}},
        {"name": "lang-timeout", "fn": "post", "args": ["game/view", {}], "responses": [OK(7)],
         "client": {"lang": "vi", "timeout": 30}},
        # agent game settings
        {"name": "currencies", "fn": "ags.get_agent_currencies", "args": [1],
         "responses": [OK([{"id": 18, "code": "USD"}])]},
        {"name": "currencies-null", "fn": "ags.get_agent_currencies", "args": ["7"], "responses": [OK(None)]},
        {"name": "settings-view", "fn": "ags.get_agent_game_settings", "args": [1, 18],
         "kwargs": {"game_code": CODE}, "responses": [OK(settings())]},
        {"name": "settings-view-opts", "fn": "ags.get_agent_game_settings", "args": ["1", 18],
         "kwargs": {"page": 2, "page_size": 50, "sort_by": "name", "sort_direction": "asc"},
         "responses": [OK(settings())]},
        {"name": "settings-view-no-data", "fn": "ags.get_agent_game_settings", "args": [1, 18],
         "responses": [{"status": "success"}]},
        {"name": "settings-update", "fn": "ags.update_agent_game_settings", "args": [1, [{"id": 1}], None],
         "responses": [OK()]},
        {"name": "settings-update-af", "fn": "ags.update_agent_game_settings",
         "args": ["1", [], {"autoFinalizationStatus": 0, "autoFinalizationDuration": "1"}], "responses": [OK()]},
        {"name": "set-paytable", "fn": "ags.set_agent_paytable", "args": ["1", 18, CODE, "AutomationTest"],
         "responses": [OK(settings()), OK()]},
        {"name": "set-paytable-cap-map", "fn": "ags.set_agent_paytable",
         "args": ["1", 18, CODE, "AutomationTest", None, 40.0],
         "responses": [OK(settings({"901": ROW_VIP, "900": ROW})), OK()]},
        {"name": "set-paytable-af", "fn": "ags.set_agent_paytable",
         "args": ["1", 18, CODE, "DEFAULT_HIGH", {"autoFinalizationStatus": 0, "autoFinalizationDuration": "1"}],
         "responses": [OK(settings()), OK()]},
        {"name": "set-paytable-unknown", "fn": "ags.set_agent_paytable", "args": ["1", 18, CODE, "Nope"],
         "responses": [OK(settings())]},
        {"name": "set-paytable-no-row", "fn": "ags.set_agent_paytable", "args": ["1", 18, "SL_X", "AutomationTest"],
         "responses": [OK(settings())]},
        {"name": "reset-paytable", "fn": "ags.reset_agent_paytable_to_default", "args": ["1", 18, CODE],
         "responses": [OK(settings([dict(ROW, payTableId=362, isDefaultPaytable=0)])), OK()]},
        {"name": "reset-paytable-high", "fn": "ags.reset_agent_paytable_to_default", "args": ["1", 18, CODE],
         "responses": [OK(settings([dict(ROW, rtpLevel="HIGH")], autoFinalizationStatus=0,
                                   autoFinalizationDuration=None)), OK()]},
        {"name": "reset-paytable-no-default", "fn": "ags.reset_agent_paytable_to_default", "args": ["1", 18, CODE],
         "responses": [OK(settings([dict(ROW, payTableList=[ROW["payTableList"][2]])]))]},
        {"name": "agent-paytable-cm", "fn": "ags.agent_paytable", "args": ["1", 18, CODE, "AutomationTest"],
         "responses": [OK(settings()), OK(), OK(settings()), OK()]},
        {"name": "agent-paytable-cm-raises", "fn": "ags.agent_paytable", "args": ["1", 18, CODE, "AutomationTest"],
         "kwargs": {"raise_in_body": True}, "responses": [OK(settings()), OK(), OK(settings()), OK()]},
        # master game paytable
        {"name": "game-detail", "fn": "ugp.get_game_detail", "args": [63, 1], "responses": [OK(GAME)]},
        {"name": "update-game", "fn": "ugp.update_game", "args": [{"id": 63, "x": [1, 2]}], "responses": [OK()]},
        {"name": "update-paytable", "fn": "ugp.update_game_paytable",
         "args": [63, 1, "AutomationTest", {"x": [1, 2], "y": "é"}], "responses": [OK(GAME), OK()]},
        {"name": "update-paytable-str", "fn": "ugp.update_game_paytable", "args": [63, 1, "DEFAULT", "{\"z\": 0}"],
         "responses": [OK(GAME), OK()]},
        {"name": "update-paytable-unknown", "fn": "ugp.update_game_paytable", "args": [63, 1, "Nope", {}],
         "responses": [OK(GAME)]},
        {"name": "ensure-existing", "fn": "ugp.ensure_game_paytable", "args": [63, 1, "AutomationTest"],
         "responses": [OK(GAME)]},
        {"name": "ensure-create-content", "fn": "ugp.ensure_game_paytable",
         "args": [63, 1, "AutomationTest", {"forced": True}, "HIGH"],
         "responses": [OK(GAME_NO_AT), OK(), OK(GAME_CREATED)]},
        {"name": "ensure-create-clone-default", "fn": "ugp.ensure_game_paytable", "args": [63, 1, "AutomationTest"],
         "responses": [OK(GAME_NO_AT), OK(), OK(GAME_CREATED)]},
        {"name": "ensure-create-clone-first", "fn": "ugp.ensure_game_paytable", "args": [63, 1, "AutomationTest"],
         "responses": [OK(GAME_NO_DEFAULT), OK(), OK(game_with(payTableInfoList=GAME_NO_DEFAULT["payTableInfoList"] + [
             {"code": "AutomationTest", "id": 1000}]))]},
        {"name": "ensure-create-empty", "fn": "ugp.ensure_game_paytable", "args": [63, 1, "AutomationTest"],
         "responses": [OK(game_with(payTableInfoList=[]))]},
        {"name": "ensure-create-no-readback", "fn": "ugp.ensure_game_paytable", "args": [63, 1, "AutomationTest", "{}"],
         "responses": [OK(GAME_NO_AT), OK(), OK(GAME_NO_AT)]},
        {"name": "ensure-create-null-id", "fn": "ugp.ensure_game_paytable", "args": [63, 1, "AutomationTest", "{}"],
         "responses": [OK(GAME_NO_AT), OK(), OK(game_with(payTableInfoList=[{"code": "AutomationTest", "id": None}]))]},
        # denominations
        {"name": "denom-list", "fn": "den.get_denomination_list", "args": [1, 31], "responses": [OK(DLIST)]},
        {"name": "denom-list-sort", "fn": "den.get_denomination_list", "args": [2, "31", "id", "asc"],
         "responses": [OK(DLIST)]},
        {"name": "denom-update", "fn": "den.update_denominations", "args": [1, 31, [{"name": "a"}]],
         "responses": [OK()]},
        {"name": "denom-create", "fn": "den.create_denomination", "args": [1, 31, "automation test TKN 3x3", [33, 11, 22]],
         "responses": [OK(DLIST), OK(), OK(DLIST_CREATED)]},
        {"name": "denom-create-listing-noverify", "fn": "den.create_denomination",
         "args": [1, 31, "new one", [1, 2]], "kwargs": {"default_bet_level": "2", "remarks": None, "verify": False,
                                                       "listing": DLIST}, "responses": [OK()]},
        {"name": "denom-create-exists", "fn": "den.create_denomination", "args": [1, 31, "tkn  3X3", [1]],
         "responses": [OK(DLIST)]},
        {"name": "denom-create-no-readback", "fn": "den.create_denomination", "args": [1, 31, "zzz", [1]],
         "kwargs": {"remarks": "r"}, "responses": [OK(DLIST), OK(), OK(DLIST)]},
        {"name": "denom-create-bad-levels", "fn": "den.create_denomination", "args": [1, 31, "zzz", [1]],
         "kwargs": {"default_bet_level": 3}, "responses": []},
        {"name": "denom-edit", "fn": "den.update_denomination", "args": [1, 31, 7],
         "kwargs": {"bet_levels": [1, 0.5], "default_bet_level": 2, "remarks": "r", "is_default": 0},
         "responses": [OK(DLIST), OK(), OK(DLIST)]},
        {"name": "denom-edit-unknown", "fn": "den.update_denomination", "args": [1, 31, 1234],
         "responses": [OK(DLIST)]},
        {"name": "denom-edit-name-noverify", "fn": "den.update_denomination", "args": [1, 31, 9],
         "kwargs": {"name": "TKN higher", "is_default": "yes", "verify": False}, "responses": [OK(DLIST), OK()]},
        {"name": "ensure-denom-existing", "fn": "den.ensure_automation_denomination", "args": [1, 31],
         "responses": [OK(DLIST_AUTO)]},
        {"name": "ensure-denom-create", "fn": "den.ensure_automation_denomination", "args": [1, 31],
         "responses": [OK(DLIST), OK(), OK(DLIST_CREATED)]},
        {"name": "ensure-denom-dup-found", "fn": "den.ensure_automation_denomination", "args": [1, 31],
         "kwargs": {"bet_levels": [5, 6], "default_bet_level": 2},
         "responses": [OK(DLIST), DUP, OK(DLIST_AUTO)]},
        {"name": "ensure-denom-dup-elsewhere", "fn": "den.ensure_automation_denomination", "args": [1, 31],
         "responses": [OK(DLIST), DUP, OK(DLIST)]},
        {"name": "ensure-denom-other-error", "fn": "den.ensure_automation_denomination", "args": [1, 31],
         "kwargs": {"prefix": "qa"},
         "responses": [OK(DLIST), {"status": "error", "message": "x", "messageCode": "INVALID_UPDATE_REQUEST"}]},
        {"name": "ensure-denom-listing", "fn": "den.ensure_automation_denomination", "args": [1, 31],
         "kwargs": {"listing": DLIST_AUTO}, "responses": []},
        {"name": "assign-game-denom", "fn": "den.assign_game_denomination", "args": [63, 1, 99],
         "kwargs": {"currency_code": "TKN"},
         "responses": [OK(GAME), OK(), OK(game_with(currencyInfoList=[dict(GAME["currencyInfoList"][0], denominationId=99)]))]},
        {"name": "assign-game-denom-mismatch", "fn": "den.assign_game_denomination", "args": [63, 1, 99],
         "kwargs": {"currency_id": 31, "is_denom_override": True}, "responses": [OK(GAME), OK(), OK(GAME)]},
        {"name": "assign-game-denom-noverify", "fn": "den.assign_game_denomination", "args": [63, 1, 99],
         "kwargs": {"currency_id": 5, "is_denom_override": 0, "verify": False}, "responses": [OK(GAME), OK()]},
        {"name": "assign-game-denom-unknown", "fn": "den.assign_game_denomination", "args": [63, 1, 99],
         "kwargs": {"currency_code": "GBP"}, "responses": [OK(GAME)]},
        {"name": "agent-denom", "fn": "den.get_agent_denomination", "args": ["1", 31, CODE],
         "responses": [OK(settings())]},
        {"name": "assign-agent-denom", "fn": "den.assign_agent_denomination", "args": ["1", 31, CODE, 99],
         "responses": [OK(settings()), OK()]},
        {"name": "assign-agent-denom-follow", "fn": "den.assign_agent_denomination", "args": ["1", 31, CODE, 99],
         "kwargs": {"override": False, "auto_finalization": {"autoFinalizationStatus": 1, "autoFinalizationDuration": "2"}},
         "responses": [OK(settings([dict(ROW, payTableId=444)])), OK()]},
        {"name": "reset-agent-denom", "fn": "den.reset_agent_denomination_to_default", "args": ["1", 31, CODE],
         "kwargs": {"denomination_id": 7}, "responses": [OK(settings([dict(ROW, denominationId=55, isDenomOverride=1)])), OK()]},
        {"name": "reset-agent-denom-keep", "fn": "den.reset_agent_denomination_to_default", "args": ["1", 31, CODE],
         "responses": [OK(settings([dict(ROW, denominationId=55, isDenomOverride=1)])), OK()]},
        {"name": "agent-auto-existing", "fn": "den.ensure_agent_automation_denomination",
         "args": [63, 1, "1", 31, CODE],
         "responses": [OK(GAME), OK(DLIST_AUTO), OK(settings()), OK(settings()), OK(), OK(DLIST_AUTO)]},
        {"name": "agent-auto-create", "fn": "den.ensure_agent_automation_denomination",
         "args": [63, 1, "1", 31, CODE], "kwargs": {"bet_levels": [33, 22, 11]},
         "responses": [OK(GAME), OK(DLIST), OK(), OK(DLIST_CREATED), OK(settings()), OK(settings()), OK(),
                       OK(DLIST_CREATED)]},
        {"name": "agent-auto-bad-level", "fn": "den.ensure_agent_automation_denomination",
         "args": [63, 1, "1", 31, CODE],
         "responses": [OK(GAME), OK(DLIST_AUTO), OK(settings()), OK(settings()), OK(),
                       OK(dict(DLIST_AUTO, denominations=[dict(AUTO, defaultBetLevel=5)]))]},
        {"name": "agent-auto-no-ladder", "fn": "den.ensure_agent_automation_denomination",
         "args": [63, 1, "1", 31, CODE],
         "responses": [OK(GAME), OK(DLIST_AUTO), OK(settings()), OK(settings()), OK(), OK(DLIST)]},
        {"name": "agent-auto-unknown-currency", "fn": "den.ensure_agent_automation_denomination",
         "args": [63, 1, "1", 77, CODE],
         "responses": [OK(GAME), OK(DLIST_AUTO), OK(settings()), OK(settings()), OK(), OK(DLIST_AUTO)]},
        {"name": "agent-auto-no-subcat", "fn": "den.ensure_agent_automation_denomination",
         "args": [63, 1, "1", 31, CODE], "responses": [OK(game_with(gameSubCategory=None))]},
    ]
    out = []
    for s in S:
        responses = [r if isinstance(r, list) else reply(r) for r in s["responses"]]
        c = {"token": T_VALID, "refresh_token": "rt-1"}
        c.update(s.get("client", {}))
        reset(responses=responses)
        refreshes = []
        kw = {k: c[k] for k in ("api_base", "lang", "timeout") if k in c}
        client = api.BoApiClient(token=c["token"], refresh_token=c["refresh_token"],
                                 on_token_refresh=lambda t, rt: refreshes.append([t, rt]), **kw)
        fn = _agent_paytable if s["fn"] == "ags.agent_paytable" else FLOWS[s["fn"]]
        args = copy.deepcopy(s.get("args", []))
        kwargs = copy.deepcopy(s.get("kwargs", {}))
        res = outcome(fn, client, *args, **kwargs)
        out.append({
            "name": s["name"], "fn": s["fn"], "client": c, "args": plain(s.get("args", [])),
            "kwargs": plain(s.get("kwargs", {})), "responses": responses, "result": res,
            "requests": HTTP["requests"], "unused": len(HTTP["queue"]), "refreshes": refreshes,
            "logs": list(LOGS), "client_after": {"token": client.token, "refresh_token": client.refresh_token},
        })
    return out


# ── BoHelper: config, token cache, resolve_paytable, the async methods ───────────────

ZEUS_CFG = """bo:
  game_id: 63
  sas_entity_id: 1
  agent_id: 1
  currency_id: 18
  game_code: SL_33_ZAPPIN_ZEUS
"""


def env_file(token=T_VALID, refresh="rt-env", extra=""):
    return f"BO_TOKEN={token}\nBO_REFRESH_TOKEN={refresh}\n{extra}"


def cache(token, refresh=None, **extra):
    d = {"token": token}
    if refresh is not None:
        d["refresh_token"] = refresh
    d.update(extra)
    return json.dumps(d, indent=2)


FIXTURES = {
    "games/zeus/paytables/base.json": '{"BaseGame": {"weights": [1, 2, 3]}, "name": "b\\u00e9"}',
    "games/zeus/paytables/fs-trigger9.json": '{"FreeSpinGame": {"3": 1, "10": 2}}',
    "games/zeus/paytables/bad.json": '{"x": ',
    "games/zeus/paytables/nan.json": '{"x": NaN, "y": -Infinity}',
    "games/zeus/paytables/UPPER.JSON": '{}',
    "games/zeus/paytables/a.b.json": '[1]',
    "games/zeus/paytables/notes.txt": 'x',
}


def helper_specs():
    sel = {"call": "select_agent_paytable", "args": ["AutomationTest"], "kwargs": {"max_payout": 40.0},
           "responses": [OK(settings()), OK()]}
    return [
        {"name": "config-full", "files": {".env.dev": env_file(), "games/zeus/env.config.dev.yml": ZEUS_CFG}},
        {"name": "config-chaos", "files": {
            ".env.dev": env_file(extra="BO_API_BASE=https://bo.example/v1/\n"),
            "games/zeus/env.config.dev.yml": ZEUS_CFG + "  paytable_code: Automation_test\n  reference_bet: 0.6\n"}},
        {"name": "config-env-override", "environ": {"BO_GAME_ID": " 70 ", "BO_AGENT_ID": "9", "BO_REFERENCE_BET": "4"},
         "files": {".env.dev": env_file(), "games/zeus/env.config.dev.yml": ZEUS_CFG}},
        {"name": "config-bad-int", "environ": {"BO_CURRENCY_ID": "abc"},
         "files": {".env.dev": env_file(), "games/zeus/env.config.dev.yml":
                   "bo:\n  game_id: '63'\n  sas_entity_id: 1.9\n  agent_id: 1\n  currency_id: 18\n  game_code: 7\n"
                   "  reference_bet: 4\n"}},
        {"name": "config-yaml-types", "files": {".env.dev": env_file(), "games/zeus/env.config.dev.yml":
                                                "bo:\n  game_id: yes\n  sas_entity_id: ''\n  agent_id: ~\n  currency_id: 'x1'\n"
                                                "  game_code: ''\n  paytable_code: on\n"}},
        {"name": "config-missing-file", "files": {".env.dev": env_file()}},
        {"name": "config-bad-yaml", "files": {".env.dev": env_file(), "games/zeus/env.config.dev.yml": "bo: [unclosed\n"}},
        {"name": "config-not-mapping", "files": {".env.dev": env_file(), "games/zeus/env.config.dev.yml": "- 1\n- 2\n"}},
        {"name": "config-bo-null", "files": {".env.dev": env_file(), "games/zeus/env.config.dev.yml": "bo:\nother: 1\n"}},
        {"name": "staging-files", "env": "staging", "files": {
            ".env.stg": env_file(token="stg-token", refresh="stg-rt"), ".env.dev": env_file(token="dev-token"),
            ".env": "BO_TOKEN=base-token\nBO_API_BASE=https://base/\n",
            "games/zeus/env.config.stg.yml": ZEUS_CFG, ".bo_token.stg.json": cache("stg-cached", "stg-cached-rt")}},
        {"name": "cache-none", "files": {".env.dev": env_file()}},
        {"name": "cache-adopted", "files": {".env.dev": env_file(token=T_OLD), ".bo_token.dev.json": cache(T_VALID, "rt-cache")}},
        {"name": "cache-adopted-no-refresh", "files": {".env.dev": env_file(token=T_OLD), ".bo_token.dev.json": cache(T_VALID)}},
        {"name": "cache-refresh-empty", "files": {".env.dev": env_file(token=T_OLD), ".bo_token.dev.json": cache(T_VALID, "")}},
        {"name": "cache-expired", "files": {".env.dev": env_file(), ".bo_token.dev.json": cache(T_DEAD, "rt-cache")}},
        {"name": "cache-older", "files": {".env.dev": env_file(token=T_VALID), ".bo_token.dev.json": cache(T_OLD, "rt-cache")}},
        {"name": "cache-opaque", "files": {".env.dev": env_file(), ".bo_token.dev.json": cache("opaque-cached", " rt-c ")}},
        {"name": "cache-env-opaque", "files": {".env.dev": env_file(token="opaque-env"), ".bo_token.dev.json": cache(T_OLD, "rt-c")}},
        {"name": "cache-whitespace", "files": {".env.dev": env_file(), ".bo_token.dev.json": cache(f"  {T_VALID}\n", "rt")}},
        {"name": "cache-empty-token", "files": {".env.dev": env_file(), ".bo_token.dev.json": cache("", "rt")}},
        {"name": "cache-null-token", "files": {".env.dev": env_file(), ".bo_token.dev.json": '{"token": null}'}},
        {"name": "cache-unreadable", "files": {".env.dev": env_file(), ".bo_token.dev.json": "{not json"}},
        {"name": "cache-list", "files": {".env.dev": env_file(), ".bo_token.dev.json": "[1]"}},
        {"name": "cache-crlf", "files": {".env.dev": env_file(), ".bo_token.dev.json": cache(T_VALID, "rt").replace("\n", "\r\n")}},
        {"name": "no-creds", "files": {"games/zeus/env.config.dev.yml": ZEUS_CFG},
         "actions": [dict(sel, responses=[]), {"call": "ensure_paytable", "args": [], "responses": []}]},
        {"name": "no-agent-config", "files": {".env.dev": env_file(), "games/zeus/env.config.dev.yml": "bo:\n  game_id: 63\n"},
         "actions": [dict(sel, responses=[]), {"call": "reset_agent_paytable", "responses": []},
                     {"call": "update_paytable", "args": ["X", {}], "responses": []},
                     {"call": "ensure_automation_denomination", "responses": []},
                     {"call": "ensure_automation_denomination", "kwargs": {"agent_id": 1, "currency_id": 31, "game_code": "G"},
                      "responses": []},
                     {"call": "ensure_paytable", "args": ["AT"], "kwargs": {"game_id": 1}, "responses": []},
                     {"call": "select_agent_paytable", "args": ["AT"],
                      "kwargs": {"agent_id": "", "currency_id": 0, "game_code": "G"}, "responses": []}]},
        {"name": "methods", "files": {".env.dev": env_file(extra="BO_API_BASE=https://bo.example/v1//\n"),
                                      "games/zeus/env.config.dev.yml": ZEUS_CFG},
         "actions": [
             sel,
             {"call": "select_agent_paytable", "args": ["AutomationTest"],
              "kwargs": {"agent_id": 2, "currency_id": 31, "game_code": CODE}, "responses": [OK(settings()), OK()]},
             {"call": "reset_agent_paytable", "responses": [OK(settings()), OK()]},
             {"call": "ensure_paytable", "args": [], "responses": [OK(GAME)]},
             {"call": "ensure_paytable", "args": ["AutomationTest"],
              "kwargs": {"paytable_content": {"f": 1}, "rtp": "LOW", "game_id": 64, "sas_entity_id": 2},
              "responses": [OK(GAME_NO_AT), OK(), OK(GAME_CREATED)]},
             {"call": "update_paytable", "args": ["AutomationTest", {"forced": [1, 2]}], "responses": [OK(GAME), OK()]},
             {"call": "update_paytable", "args": ["DEFAULT", "{}", 70, 3], "responses": [OK(GAME), OK()]},
             {"call": "ensure_automation_denomination", "kwargs": {"currency_id": 31, "bet_levels": [33, 11, 22]},
              "responses": [OK(GAME), OK(DLIST), OK(), OK(DLIST_CREATED), OK(settings()), OK(settings()), OK(),
                            OK(DLIST_CREATED)]},
             {"call": "assign_agent_denomination", "args": [55], "responses": [OK(settings()), OK()]},
             {"call": "reset_agent_denomination", "kwargs": {"denomination_id": 7}, "responses": [OK(settings()), OK()]},
             {"call": "reset_agent_denomination", "responses": [OK(settings()), OK()]},
         ]},
        {"name": "refresh-persists", "files": {".env.dev": env_file(token=T_SOON), "games/zeus/env.config.dev.yml": ZEUS_CFG},
         "actions": [{"call": "reset_agent_paytable",
                      "responses": [OK({"token": T_NEW, "refreshToken": "rt-rotated"}), OK(settings()), OK()]},
                     {"call": "persist_token", "args": ["manual-token"], "responses": []},
                     {"call": "persist_token", "args": ["manual-2", "rt-manual"], "responses": []}]},
        {"name": "refresh-persists-same-rt", "files": {".env.dev": env_file(token=T_DEAD), "games/zeus/env.config.dev.yml": ZEUS_CFG},
         "actions": [{"call": "assign_agent_denomination", "args": [55],
                      "responses": [OK({"token": T_NEW}), OK(settings()), OK()]},
                     {"call": "clear_token_cache", "responses": []},
                     {"call": "clear_token_cache", "responses": []}]},
        {"name": "resolve-paytable", "files": dict(FIXTURES, **{".env.dev": env_file()}),
         "actions": [{"call": "resolve_paytable", "kwargs": kw, "responses": []} for kw in [
             {"inline": {"x": 1}}, {"inline": "{\"raw\": 1}"}, {"inline": False, "ref": "base"}, {"inline": 0},
             {"ref": "base"}, {"ref": "base.json"}, {"ref": "fs-trigger9"}, {"ref": "a.b"}, {"ref": "missing"},
             {"ref": ""}, {}, {"ref": "bad"}, {"ref": "nan"}, {"ref": "UPPER.JSON"}]]},
        {"name": "resolve-paytable-no-dir", "files": {".env.dev": env_file()},
         "actions": [{"call": "resolve_paytable", "kwargs": {"ref": "base"}, "responses": []}]},
        {"name": "int-env", "files": {}, "environ": {"BO_GAME_ID": " 12 ", "BO_SAS_ENTITY_ID": "x"},
         "actions": [{"call": "_int_env", "args": [n], "responses": []}
                     for n in ["BO_GAME_ID", "BO_SAS_ENTITY_ID", "BO_AGENT_ID"]]},
    ]


def _norm(text, bases):
    if not isinstance(text, str):
        return text
    for b in bases:
        idx = text.lower().find(b.lower())
        while idx >= 0:
            text = text[:idx] + "<BASE>" + text[idx + len(b):]
            idx = text.lower().find(b.lower())
    return text.replace("\\", "/") if "<BASE>" in text else text


def _norm_rec(v, bases):
    if isinstance(v, str):
        return _norm(v, bases)
    if isinstance(v, list):
        return [_norm_rec(x, bases) for x in v]
    if isinstance(v, dict):
        return {k: _norm_rec(x, bases) for k, x in v.items()}
    return v


def helper_cases():
    out = []
    saved = {k: os.environ.get(k) for k in BO_ENV_KEYS}
    for spec in helper_specs():
        tmp = tempfile.mkdtemp(prefix="bo-baseline-")
        bases = sorted({str(Path(tmp).resolve()), tmp, os.path.realpath(tmp)}, key=len, reverse=True)
        try:
            for rel, text in spec.get("files", {}).items():
                p = Path(tmp, rel)
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
            for k in BO_ENV_KEYS:
                os.environ.pop(k, None)
            os.environ.update(spec.get("environ", {}))
            reset()
            env = spec.get("env", "dev")
            try:
                h = hb.BoHelper("zeus", env, base_dir=tmp)
                construct = {"ok": True}
            except Exception as e:  # noqa: BLE001 -- the exception is the recorded answer
                h = None
                construct = {"err": type(e).__name__, "msg": str(e)}
            rec = {"name": spec["name"], "env": env, "files": spec.get("files", {}), "environ": spec.get("environ", {}),
                   "construct": construct, "construct_logs": list(LOGS), "actions": []}
            if h is not None:
                rec["attrs"] = {
                    "game_id": h.game_id, "sas_entity_id": h.sas_entity_id, "agent_id": h.agent_id,
                    "currency_id": h.currency_id, "game_code": h.game_code, "reference_bet": h.reference_bet,
                    "paytable_code": h.paytable_code, "token": h._token, "refresh_token": h._refresh_token,
                    "api_base": h._api_base, "token_cache": h._token_cache_path.name,
                }
                for a in spec.get("actions", []):
                    responses = [r if isinstance(r, list) else reply(r) for r in a.get("responses", [])]
                    reset(responses=responses)
                    args = copy.deepcopy(a.get("args", []))
                    kwargs = copy.deepcopy(a.get("kwargs", {}))
                    call = a["call"]
                    if call == "_int_env":
                        res = outcome(hb.BoHelper._int_env, *args)
                    elif call in ("resolve_paytable", "persist_token", "clear_token_cache"):
                        res = outcome(getattr(h, "_persist_token" if call == "persist_token" else call), *args, **kwargs)
                    else:
                        res = asyncio.run(aoutcome(lambda: getattr(h, call)(*args, **kwargs)))
                    cache_path = h._token_cache_path
                    rec["actions"].append({
                        "call": call, "args": plain(a.get("args", [])), "kwargs": plain(a.get("kwargs", {})),
                        "responses": responses, "result": res, "requests": HTTP["requests"],
                        "unused": len(HTTP["queue"]), "logs": list(LOGS),
                        "token_cache": cache_path.read_bytes().decode("utf-8") if cache_path.exists() else None,
                        "client": ({"token": h._client_instance.token, "refresh_token": h._client_instance.refresh_token}
                                   if h._client_instance else None),
                    })
            out.append(_norm_rec(rec, bases))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return out


# ── real paytable fixtures -> the payTable string sent to BO ─────────────────────────

def fixture_strings():
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "games", "*", "paytables", "*.json"))):
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        with open(p, encoding="utf-8") as fh:
            s = ugp._as_paytable_string(json.load(fh))
        out.append({"path": rel, "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest(), "length": len(s)})
    return out


def main():
    data = {
        "now": NOW,
        "constants": {
            "API_BASE": api.API_BASE, "TOKEN_EXPIRED": api.TOKEN_EXPIRED, "UNAUTHORIZED": api.UNAUTHORIZED,
            "SUCCESS_STATUSES": list(api.SUCCESS_STATUSES), "REFRESH_SKEW_S": api.REFRESH_SKEW_S,
            "DEFAULT_AUTO_FINALIZATION": ags.DEFAULT_AUTO_FINALIZATION,
            "AUTOMATION_PREFIX": den.AUTOMATION_PREFIX, "AUTOMATION_BET_LEVELS": list(den.AUTOMATION_BET_LEVELS),
            "AUTOMATION_DEFAULT_LEVEL": den.AUTOMATION_DEFAULT_LEVEL,
            "DENOM_OVERRIDDEN": den.DENOM_OVERRIDDEN, "DENOM_FOLLOWING_DEFAULT": den.DENOM_FOLLOWING_DEFAULT,
        },
        "jwt": jwt_cases(),
        "pure": pure_cases(),
        "max_payout": max_payout_cases(),
        "glue": setup_cases(),
        "flows": flow_cases(),
        "helper": helper_cases(),
        "fixtures": fixture_strings(),
    }
    path = os.path.join(OUT, "bo.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    n = (len(data["jwt"]) + len(data["pure"]) + len(data["max_payout"]) + len(data["glue"]["setup"])
         + len(data["glue"]["reset"]) + len(data["glue"]["read_base_bet"]) + len(data["flows"])
         + sum(1 + len(h["actions"]) for h in data["helper"]) + len(data["fixtures"]))
    print(f"wrote {os.path.relpath(path, ROOT)} ({n} cases)")


if __name__ == "__main__":
    main()
