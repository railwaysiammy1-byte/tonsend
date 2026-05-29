import asyncio
import time
import requests

from fastapi import FastAPI
from pydantic import BaseModel
from web3 import Web3

from pytoniq import LiteBalancer, WalletV5R1
from pytoniq_core import begin_cell

# =========================================================
# APP
# =========================================================

app = FastAPI(
    title="TON Full Payment Gateway Auto Deploy"
)

# =========================================================
# GLOBALS
# =========================================================

ton_client = None

wallet_lock = asyncio.Lock()

# =========================================================
# TON SETTINGS
# =========================================================

# deploy + tx + gas reserve
AUTO_FEE_BUFFER_TON = 0.05

# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
async def startup():

    global ton_client

    ton_client = LiteBalancer.from_mainnet_config(
        trust_level=1
    )

    await ton_client.start_up()

    print("TON client connected")


# =========================================================
# SHUTDOWN
# =========================================================

@app.on_event("shutdown")
async def shutdown():

    global ton_client

    try:

        if ton_client:
            await ton_client.close_all()

    except Exception as e:

        print("Shutdown error:", e)


# =========================================================
# ROOT
# =========================================================

@app.get("/")
async def root():

    return {
        "ok": True,
        "service": "TON Auto Deploy Gateway",
        "status": "running",
        "timestamp": int(time.time())
    }


# =========================================================
# BSC PRICE ORACLE
# =========================================================

bsc_rpc = "https://bsc-dataseed.binance.org/"

web3 = Web3(
    Web3.HTTPProvider(
        bsc_rpc,
        request_kwargs={
            "timeout": 15
        }
    )
)

pool_address = web3.to_checksum_address(
    "0x819a26D0C6F3af2B9fe4E9c4BcaC04fCB3ea7f2a"
)

pool_abi = [
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {
                "name": "_reserve0",
                "type": "uint112"
            },
            {
                "name": "_reserve1",
                "type": "uint112"
            },
            {
                "name": "_blockTimestampLast",
                "type": "uint32"
            }
        ],
        "type": "function"
    }
]

contract = web3.eth.contract(
    address=pool_address,
    abi=pool_abi
)


def pancake_price():

    reserves = contract.functions.getReserves().call()

    reserve_usdt = reserves[0] / (10 ** 18)

    reserve_ton = reserves[1] / (10 ** 9)

    price = reserve_usdt / reserve_ton

    if price <= 0:
        raise Exception("Invalid pancake price")

    return float(price)


def diadata_price():

    url = (
        "https://api.diadata.org/v1/assetQuotation/"
        "Ton/0x0000000000000000000000000000000000000000"
    )

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
# RESPONSE
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
# REQUEST MODEL
# =========================================================

class SendRequest(BaseModel):

    mnemonic: list[str]

    to_address: str

    amount_ton: float

    memo: str | None = None


# =========================================================
# TRACK TX
# =========================================================

async def track_tx(client, wallet, old_seqno, timeout=150):

    start = time.time()

    while True:

        try:

            seqno = await wallet.get_seqno()

            if seqno > old_seqno:

                txs = await client.get_transactions(
                    wallet.address,
                    count=10
                )

                if txs:

                    try:
                        return txs[0].cell.hash.hex()

                    except Exception:
                        return None

        except Exception:
            pass

        if time.time() - start > timeout:
            return None

        await asyncio.sleep(3)


# =========================================================
# CHECK ACTIVE
# =========================================================

async def is_wallet_active(wallet):

    try:

        await wallet.get_seqno()

        return True

    except Exception:

        return False


# =========================================================
# AUTO DEPLOY
# =========================================================

async def auto_deploy_wallet(wallet):

    active = await is_wallet_active(wallet)

    if active:

        return {
            "success": True,
            "deployed": False,
            "message": "Wallet already active"
        }

    try:

        # deploy by self transfer
        await wallet.transfer(
            destination=wallet.address,
            amount=int(0.01 * 1e9),
            body=None
        )

    except Exception as e:

        return {
            "success": False,
            "message": "Deploy transaction failed",
            "error": str(e)
        }

    # wait active
    for _ in range(30):

        try:

            seqno = await wallet.get_seqno()

            return {
                "success": True,
                "deployed": True,
                "seqno": seqno,
                "message": "Wallet deployed successfully"
            }

        except Exception:

            await asyncio.sleep(2)

    return {
        "success": False,
        "message": "Wallet deploy timeout"
    }


# =========================================================
# MAIN PAYMENT
# =========================================================

async def process_payment(req: SendRequest):

    global ton_client

    client = ton_client

    if client is None:

        return response(
            False,
            "TON client not ready",
            None,
            503
        )

    # =====================================================
    # MEMO
    # =====================================================

    body = None

    memo_value = None

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

    # =====================================================
    # LOAD WALLET
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
            "Wallet initialization failed",
            {
                "error": str(e)
            },
            400
        )

    wallet_addr = str(wallet.address)

    # =====================================================
    # PRICE
    # =====================================================

    ton_price = get_ton_price()

    requested_amount = float(req.amount_ton)

    requested_nano = int(requested_amount * 1e9)

    # =====================================================
    # BALANCE
    # =====================================================

    try:

        before_balance = await wallet.get_balance()

    except Exception as e:

        return response(
            False,
            "Balance fetch failed",
            {
                "error": str(e)
            },
            502
        )

    balance_ton = before_balance / 1e9

    # =====================================================
    # AUTO ADJUST FEES
    # =====================================================

    reserve_nano = int(AUTO_FEE_BUFFER_TON * 1e9)

    sendable = before_balance - reserve_nano

    if sendable <= 0:

        return response(
            False,
            "Insufficient balance for deploy and fees",
            {
                "wallet": wallet_addr,
                "balance_ton": round(balance_ton, 9),
                "required_fee_buffer_ton": AUTO_FEE_BUFFER_TON
            },
            402
        )

    # auto adjust amount
    actual_send_nano = min(
        requested_nano,
        sendable
    )

    actual_send_ton = actual_send_nano / 1e9

    # =====================================================
    # LOCK
    # =====================================================

    async with wallet_lock:

        # =================================================
        # AUTO DEPLOY
        # =================================================

        deploy_result = await auto_deploy_wallet(wallet)

        if not deploy_result["success"]:

            return response(
                False,
                "Wallet auto deploy failed",
                deploy_result,
                500
            )

        # =================================================
        # GET SEQNO
        # =================================================

        try:

            old_seqno = await wallet.get_seqno()

        except Exception as e:

            return response(
                False,
                "Seqno fetch failed",
                {
                    "error": str(e)
                },
                502
            )

        # =================================================
        # SEND TX
        # =================================================

        try:

            await wallet.transfer(
                destination=req.to_address,
                amount=actual_send_nano,
                body=body
            )

        except Exception as e:

            return response(
                False,
                "Transaction failed",
                {
                    "error": str(e)
                },
                500
            )

        # =================================================
        # TRACK TX
        # =================================================

        txid = await track_tx(
            client,
            wallet,
            old_seqno
        )

    # =====================================================
    # FINAL BALANCE
    # =====================================================

    try:

        after_balance = await wallet.get_balance()

    except Exception:

        after_balance = (
            before_balance
            - actual_send_nano
        )

    # =====================================================
    # FEES
    # =====================================================

    fee_nano = (
        before_balance
        - after_balance
        - actual_send_nano
    )

    if fee_nano < 0:
        fee_nano = 0

    fee_ton = fee_nano / 1e9

    # =====================================================
    # SUCCESS
    # =====================================================

    return response(
        True,
        "Transaction completed",
        {
            "wallet": wallet_addr,

            "to_address": req.to_address,

            "memo": memo_value,

            "txid": txid,

            "hash_status": (
                "confirmed"
                if txid
                else "pending"
            ),

            "requested_amount_ton": round(
                requested_amount,
                9
            ),

            "actual_sent_ton": round(
                actual_send_ton,
                9
            ),

            "before_balance_ton": round(
                balance_ton,
                9
            ),

            "after_balance_ton": round(
                after_balance / 1e9,
                9
            ),

            "fee_ton": round(
                fee_ton,
                9
            ),

            "ton_price_usd": round(
                ton_price,
                6
            ),

            "estimated_sent_usd": round(
                actual_send_ton * ton_price,
                6
            ),

            "deploy_checked": True
        }
    )


# =========================================================
# API
# =========================================================

@app.post("/send")
async def send(req: SendRequest):

    return await process_payment(req)
