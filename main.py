import asyncio
import time
import requests
from fastapi import FastAPI
from pydantic import BaseModel
from web3 import Web3
from pytoniq import LiteBalancer, WalletV5R1
from pytoniq_core import begin_cell

app = FastAPI(title="TON Full Payment Gateway")

# =========================================================
# BSC PANCAKESWAP PRICE ORACLE
# =========================================================
bsc_rpc = "https://bsc-dataseed.binance.org/"
web3 = Web3(Web3.HTTPProvider(bsc_rpc))

pool_address = web3.to_checksum_address(
    "0x819a26D0C6F3af2B9fe4E9c4BcaC04fCB3ea7f2a"
)

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

    return reserve_usdt / reserve_ton


def diadata_price():
    url = "https://api.diadata.org/v1/assetQuotation/Ton/0x0000000000000000000000000000000000000000"

    r = requests.get(url, timeout=10)
    data = r.json()

    return float(data["Price"])


def get_ton_price():
    try:
        price = pancake_price()

        if price > 0:
            return price

    except Exception:
        pass

    try:
        price = diadata_price()

        if price > 0:
            return price

    except Exception:
        pass

    return 0.0


# =========================================================
# REQUEST MODEL
# =========================================================
class SendRequest(BaseModel):
    mnemonic: list[str]
    to_address: str
    amount_ton: float
    memo: str | None = None


# =========================================================
# RESPONSE WRAPPER
# =========================================================
def response(ok, message, data=None, code=200):
    return {
        "ok": ok,
        "message": message,
        "status_code": code,
        "timestamp": int(time.time()),
        "data": data
    }


# =========================================================
# TX TRACKER
# =========================================================
async def track_tx(client, wallet, old_seqno, timeout=150):
    start = time.time()

    while True:
        try:
            seqno = await wallet.get_seqno()

            if seqno > old_seqno:
                txs = await client.get_transactions(wallet.address, count=10)

                if txs:
                    return txs[0].cell.hash.hex()

        except Exception:
            pass

        if time.time() - start > timeout:
            return None

        await asyncio.sleep(5)


# =========================================================
# MAIN PAYMENT FUNCTION
# =========================================================
async def process_payment(req: SendRequest):

    body = None
    memo_value = "PAYMENT"

    if req.memo and req.memo.strip():
        memo_value = req.memo.strip()

    memo_bytes = memo_value.encode("utf-8")

    if len(memo_bytes) > 123:
        return response(
            False,
            "Memo too long",
            {
                "memo_bytes": len(memo_bytes),
                "max_bytes": 123
            },
            400
        )

    body = (
        begin_cell()
        .store_uint(0, 32)
        .store_string(memo_value)
        .end_cell()
    )

    client = LiteBalancer.from_mainnet_config(trust_level=2)

    try:
        await client.start_up()

        # =====================================================
        # WALLET LOAD
        # =====================================================
        try:
            wallet = await WalletV5R1.from_mnemonic(
                provider=client,
                mnemonics=req.mnemonic,
                network_global_id=-239
            )

        except Exception as e:
            return response(
                False,
                "Invalid mnemonic / seed phrase",
                {"error": str(e)},
                400
            )

        wallet_addr = str(wallet.address)

        # =====================================================
        # PRICE
        # =====================================================
        ton_price = get_ton_price()

        amount_ton = float(req.amount_ton)
        amount_usd = amount_ton * ton_price

        # =====================================================
        # BALANCE
        # =====================================================
        try:
            before_balance = await wallet.get_balance()

        except Exception as e:
            return response(
                False,
                "Balance fetch failed",
                {"error": str(e)},
                502
            )

        balance_ton = before_balance / 1e9

        required = int(amount_ton * 1e9)

        if before_balance < required:
            return response(
                False,
                "Insufficient balance",
                {
                    "wallet": wallet_addr,
                    "balance_ton": round(balance_ton, 9),
                    "balance_usd": round(balance_ton * ton_price, 6),

                    "required_ton": round(amount_ton, 9),
                    "required_usd": round(amount_usd, 6),

                    "ton_price_usd": round(ton_price, 6)
                },
                402
            )

        # =====================================================
        # SEQNO
        # =====================================================
        try:
            old_seqno = await wallet.get_seqno()

        except Exception as e:
            return response(
                False,
                "Seqno fetch failed",
                {"error": str(e)},
                502
            )

        # =====================================================
        # SEND TX
        # =====================================================
        try:
            await wallet.transfer(
                destination=req.to_address,
                amount=required,
                body=body
            )

        except Exception as e:
            return response(
                False,
                "Transaction failed",
                {"error": str(e)},
                500
            )

        # =====================================================
        # TRACK TX
        # =====================================================
        txid = await track_tx(
            client,
            wallet,
            old_seqno,
            timeout=150
        )

        # =====================================================
        # FINAL BALANCE
        # =====================================================
        after_balance = await wallet.get_balance()

        after_balance_ton = after_balance / 1e9

        # =====================================================
        # FEES
        # =====================================================
        fee_ton = (
            before_balance
            - after_balance
            - required
        ) / 1e9

        if fee_ton < 0:
            fee_ton = 0

        fee_usd = fee_ton * ton_price

        # =====================================================
        # SUCCESS RESPONSE
        # =====================================================
        return response(
            True,
            "Transaction completed",
            {
                "success": True,

                "wallet": wallet_addr,
                "to_address": req.to_address,

                "memo": memo_value,

                "txid": txid,
                "hash_status": (
                    "confirmed"
                    if txid
                    else "pending"
                ),

                "amount_ton": round(amount_ton, 9),
                "amount_usd": round(amount_usd, 6),

                "before_balance_ton": round(balance_ton, 9),
                "before_balance_usd": round(balance_ton * ton_price, 6),

                "after_balance_ton": round(after_balance_ton, 9),
                "after_balance_usd": round(after_balance_ton * ton_price, 6),

                "fee_ton": round(fee_ton, 9),
                "fee_usd": round(fee_usd, 6),

                "ton_price_usd": round(ton_price, 6)
            }
        )

    except Exception as e:
        return response(
            False,
            "Server error",
            {"error": str(e)},
            500
        )

    finally:
        try:
            await client.close_all()
        except Exception:
            pass


# =========================================================
# API ENDPOINT
# =========================================================
@app.post("/send")
async def send(req: SendRequest):
    return await process_payment(req)
