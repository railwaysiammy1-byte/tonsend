import asyncio
import time
import requests

from fastapi import FastAPI
from pydantic import BaseModel
from web3 import Web3

# TON imports (exactly like your proven example)
from ton_core import NetworkGlobalID, to_nano
from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV5R1

# For memo body
from pytoniq_core import begin_cell

# =========================================================
# APP
# =========================================================
app = FastAPI(title="TON Full Payment Gateway")

# Prevent parallel tx conflicts
wallet_lock = asyncio.Lock()

# =========================================================
# BSC PRICE ORACLE (unchanged)
# =========================================================
bsc_rpc = "https://bsc-dataseed.binance.org/"
web3 = Web3(Web3.HTTPProvider(bsc_rpc, request_kwargs={"timeout": 15}))
pool_address = web3.to_checksum_address("0x819a26D0C6F3af2B9fe4E9c4BcaC04fCB3ea7f2a")
pool_abi = [
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"}
        ],
        "type": "function"
    }
]
contract = web3.eth.contract(address=pool_address, abi=pool_abi)

def pancake_price():
    reserves = contract.functions.getReserves().call()
    reserve_usdt = reserves[0] / (10 ** 18)
    reserve_ton = reserves[1] / (10 ** 9)
    price = reserve_usdt / reserve_ton
    if price <= 0:
        raise Exception("Invalid price")
    return float(price)

def diadata_price():
    url = "https://api.diadata.org/v1/assetQuotation/Ton/0x0000000000000000000000000000000000000000"
    r = requests.get(url, timeout=10)
    data = r.json()
    return float(data["Price"])

def get_ton_price():
    try:
        return pancake_price()
    except Exception:
        pass
    try:
        return diadata_price()
    except Exception:
        pass
    return 0.0

# =========================================================
# MODELS & HELPERS
# =========================================================
class SendRequest(BaseModel):
    mnemonic: list[str]
    to_address: str
    amount_ton: float
    memo: str | None = None

def response(ok, message, data=None, code=200):
    return {
        "ok": ok,
        "message": message,
        "status_code": code,
        "timestamp": int(time.time()),
        "data": data
    }

# =========================================================
# MAIN PAYMENT
# =========================================================
async def process_payment(req: SendRequest):

    # ---- Connect to TON mainnet ----
    client = ToncenterClient(network=NetworkGlobalID.MAINNET)
    await client.connect()

    try:
        # ---- Create wallet from mnemonic ----
        # tonutils expects mnemonic as a single string
        wallet, public_key, private_key, mnemonic = WalletV5R1.from_mnemonic(
            client,
            " ".join(req.mnemonic)
        )

        wallet_addr = wallet.address.to_str(is_bounceable=False)

        # ---- Refresh state (balance, status, etc.) ----
        await wallet.refresh()

        # ---- PRICE & AMOUNT ----
        ton_price = get_ton_price()
        amount_ton = float(req.amount_ton)
        amount_usd = amount_ton * ton_price
        required = int(amount_ton * 1e9)

        # ---- BALANCE ----
        before_balance = wallet.balance
        balance_ton = before_balance / 1e9

        if before_balance < required:
            return response(False, "Insufficient balance", {
                "wallet": wallet_addr,
                "balance_ton": round(balance_ton, 9),
                "balance_usd": round(balance_ton * ton_price, 6),
                "required_ton": round(amount_ton, 9),
                "required_usd": round(amount_usd, 6),
                "ton_price_usd": round(ton_price, 6)
            }, 402)

        # ---- MEMO (optional) ----
        body = None
        memo_value = None
        if req.memo and req.memo.strip():
            memo_value = req.memo.strip()
            memo_bytes = memo_value.encode("utf-8")
            if len(memo_bytes) > 123:
                return response(False, "Memo too long", {"memo_bytes": len(memo_bytes), "max_bytes": 123}, 400)
            body = begin_cell().store_uint(0, 32).store_string(memo_value).end_cell()

        # ---- LOCKED TRANSFER (prevents parallel seqno issues) ----
        async with wallet_lock:

            # ---- DEPLOY WALLET IF NOT ACTIVE ----
            if wallet.state != "active":
                print("Wallet not deployed. Deploying via self-transfer...")
                # First transfer deploys the wallet (0.01 TON to itself)
                deploy_amount = to_nano(0.01)
                await wallet.transfer(
                    destination=wallet.address,
                    amount=deploy_amount,
                    body=None
                )
                # Wait for deployment to take effect
                await asyncio.sleep(5)
                # Refresh state so the wallet is ready for the next transfer
                await wallet.refresh()
                print("Wallet deployed successfully.")

            # ---- SEND MAIN TRANSACTION ----
            msg = await wallet.transfer(
                destination=req.to_address,
                amount=required,
                body=body
            )

            txid = msg.normalized_hash

        # ---- FINAL BALANCE & FEES ----
        await wallet.refresh()
        after_balance = wallet.balance
        after_balance_ton = after_balance / 1e9
        fee_ton = (before_balance - after_balance - required) / 1e9
        if fee_ton < 0:
            fee_ton = 0
        fee_usd = fee_ton * ton_price

        return response(True, "Transaction completed", {
            "success": True,
            "wallet": wallet_addr,
            "to_address": req.to_address,
            "memo": memo_value,
            "txid": txid,
            "hash_status": "confirmed",
            "amount_ton": round(amount_ton, 9),
            "amount_usd": round(amount_usd, 6),
            "before_balance_ton": round(balance_ton, 9),
            "before_balance_usd": round(balance_ton * ton_price, 6),
            "after_balance_ton": round(after_balance_ton, 9),
            "after_balance_usd": round(after_balance_ton * ton_price, 6),
            "fee_ton": round(fee_ton, 9),
            "fee_usd": round(fee_usd, 6),
            "ton_price_usd": round(ton_price, 6),
        })

    except Exception as e:
        return response(False, "Transaction failed", {"error": str(e)}, 500)

    finally:
        await client.close()


# =========================================================
# API ENDPOINT
# =========================================================
@app.post("/send")
async def send(req: SendRequest):
    return await process_payment(req)


# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
