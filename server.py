"""
賽克斯策略自動交易伺服器
接收 TradingView Webhook → 自動在 OKX 下單
"""

import os
import json
import hmac
import hashlib
import base64
import time
import math
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ══════════════════════════════════════════
# OKX API 設定（填入您的 API 金鑰）
# ══════════════════════════════════════════
OKX_API_KEY    = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "saix_secret_2024")

OKX_BASE_URL   = "https://www.okx.com"

# 交易設定
MARGIN_RATIO   = 0.10   # 每筆用總資金 10%
MAX_LEVERAGE   = 125    # OKX 最大槓桿（上限）
SL_DIVISOR     = 50     # 槓桿 = 50 ÷ 停損%
AUTO_TRADE_ONLY_ALIGNED = False  # False = 趨勢不一致也下單（加通知）


# ══════════════════════════════════════════
# OKX API 工具函式
# ══════════════════════════════════════════
def get_timestamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

def sign(timestamp, method, path, body=""):
    msg = timestamp + method + path + body
    mac = hmac.new(
        OKX_SECRET_KEY.encode(),
        msg.encode(),
        hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()

def okx_request(method, path, body=None):
    timestamp = get_timestamp()
    body_str  = json.dumps(body) if body else ""
    headers   = {
        "OK-ACCESS-KEY":        OKX_API_KEY,
        "OK-ACCESS-SIGN":       sign(timestamp, method, path, body_str),
        "OK-ACCESS-TIMESTAMP":  timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type":         "application/json",
    }
    url = OKX_BASE_URL + path
    if method == "GET":
        resp = requests.get(url, headers=headers)
    else:
        resp = requests.post(url, headers=headers, data=body_str)
    return resp.json()

def get_account_equity():
    """取得帳戶總權益（USDT）"""
    data = okx_request("GET", "/api/v5/account/balance")
    try:
        return float(data["data"][0]["totalEq"])
    except:
        return 0.0

def get_max_leverage(inst_id):
    """取得該幣種最大槓桿"""
    data = okx_request("GET", f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
    try:
        return int(data["data"][0]["lever"])
    except:
        return MAX_LEVERAGE

def get_instrument_info(inst_id):
    """取得合約基本資訊（最小下單量、合約面值）"""
    data = okx_request("GET", f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
    try:
        info = data["data"][0]
        return {
            "ct_val":   float(info["ctVal"]),    # 合約面值
            "lot_sz":   float(info["lotSz"]),    # 最小下單單位
            "min_sz":   float(info["minSz"]),    # 最小張數
        }
    except:
        return {"ct_val": 1, "lot_sz": 1, "min_sz": 1}

def set_leverage(inst_id, leverage, pos_side):
    """設定槓桿"""
    body = {
        "instId":  inst_id,
        "lever":   str(leverage),
        "mgnMode": "isolated",
        "posSide": pos_side,
    }
    return okx_request("POST", "/api/v5/account/set-leverage", body)

def place_order(inst_id, side, pos_side, sz, entry_price, sl_price, tp_price, leverage):
    """下單（限價）+ 設止盈止損"""

    # 1. 設定槓桿
    set_leverage(inst_id, leverage, pos_side)

    # 2. 主單
    order_body = {
        "instId":  inst_id,
        "tdMode":  "isolated",
        "side":    side,
        "posSide": pos_side,
        "ordType": "limit",
        "px":      str(entry_price),
        "sz":      str(sz),
    }
    order_resp = okx_request("POST", "/api/v5/trade/order", order_body)
    print(f"[下單] {inst_id} {side} {sz}張 @ {entry_price} → {order_resp}")

    # 3. 止盈止損
    algo_body = {
        "instId":     inst_id,
        "tdMode":     "isolated",
        "side":       "sell" if side == "buy" else "buy",
        "posSide":    pos_side,
        "ordType":    "oco",
        "sz":         str(sz),
        "tpTriggerPx": str(tp_price),
        "tpOrdPx":     "-1",
        "slTriggerPx": str(sl_price),
        "slOrdPx":     "-1",
    }
    algo_resp = okx_request("POST", "/api/v5/trade/order-algo", algo_body)
    print(f"[止盈止損] TP={tp_price} SL={sl_price} → {algo_resp}")

    return order_resp, algo_resp


# ══════════════════════════════════════════
# 計算下單參數
# ══════════════════════════════════════════
def calculate_order_params(equity, entry, sl, sl_pct, side, inst_info, max_lev):
    """
    計算槓桿、下單張數、止盈價
    """
    # 槓桿 = 50 ÷ 停損%（上限取交易所最大值）
    leverage = min(math.floor(SL_DIVISOR / sl_pct), max_lev)
    leverage = max(leverage, 1)

    # 保證金 = 總資金 × 10%
    margin = equity * MARGIN_RATIO

    # 下單名義金額 = 保證金 × 槓桿
    notional = margin * leverage

    # 計算張數（向下取整到最小單位）
    ct_val  = inst_info["ct_val"]
    lot_sz  = inst_info["lot_sz"]
    sz_raw  = notional / (entry * ct_val)
    sz      = math.floor(sz_raw / lot_sz) * lot_sz
    sz      = max(sz, inst_info["min_sz"])

    # 止盈價（1:1 盈虧）
    distance = abs(entry - sl)
    if side == "buy":
        tp = round(entry + distance, 6)
    else:
        tp = round(entry - distance, 6)

    return {
        "leverage": leverage,
        "margin":   round(margin, 2),
        "sz":       sz,
        "tp":       tp,
        "sl":       sl,
    }


# ══════════════════════════════════════════
# Webhook 端點
# ══════════════════════════════════════════
@app.route("/webhook", methods=["POST"])
def webhook():
    # 驗證來源
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json(force=True)
        print(f"\n[收到信號] {datetime.now()} → {data}")
    except:
        return jsonify({"error": "Invalid JSON"}), 400

    symbol       = data.get("symbol", "")         # 例：BTCUSDT
    side         = data.get("side", "")           # buy / sell
    entry        = float(data.get("entry", 0))
    sl           = float(data.get("sl", 0))
    sl_pct       = float(data.get("sl_pct", 0))
    trend_aligned = data.get("trend_aligned", False)

    if not symbol or not side or not entry or not sl or not sl_pct:
        return jsonify({"error": "Missing fields"}), 400

    # 轉換成 OKX 格式（BTCUSDT → BTC-USDT-SWAP）
    if symbol.endswith("USDT"):
        base   = symbol.replace("USDT", "")
        inst_id = f"{base}-USDT-SWAP"
    else:
        inst_id = f"{symbol}-SWAP"

    pos_side = "long" if side == "buy" else "short"

    # 取得帳戶資訊
    equity    = get_account_equity()
    max_lev   = get_max_leverage(inst_id)
    inst_info = get_instrument_info(inst_id)

    # 計算下單參數
    params = calculate_order_params(equity, entry, sl, sl_pct, side, inst_info, max_lev)

    print(f"[計算結果] 槓桿={params['leverage']}x | 張數={params['sz']} | TP={params['tp']} | SL={params['sl']} | 保證金={params['margin']} USDT")

    # 趨勢不一致 → 只通知，不自動下單
    if not trend_aligned and AUTO_TRADE_ONLY_ALIGNED:
        print(f"[跳過] 趨勢不一致，不下單")
        return jsonify({
            "status":  "skipped",
            "reason":  "trend_not_aligned",
            "params":  params,
            "inst_id": inst_id,
        })

    # 執行下單
    order_resp, algo_resp = place_order(
        inst_id    = inst_id,
        side       = side,
        pos_side   = pos_side,
        sz         = params["sz"],
        entry_price= entry,
        sl_price   = params["sl"],
        tp_price   = params["tp"],
        leverage   = params["leverage"],
    )

    return jsonify({
        "status":       "success",
        "inst_id":      inst_id,
        "leverage":     params["leverage"],
        "sz":           params["sz"],
        "entry":        entry,
        "tp":           params["tp"],
        "sl":           params["sl"],
        "margin_used":  params["margin"],
        "trend_aligned":trend_aligned,
        "order":        order_resp,
        "algo":         algo_resp,
    })

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "online", "bot": "賽克斯策略自動交易"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
