```python
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
app = FastAPI(title="TON Full Payment Gateway")

# =========================================================
# GLOBAL TON CLIENT
# =========================================================
ton_client = None

# Prevent seqno conflicts
wallet_lock = asyncio.Lock()

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
        "status": "running",
        "service": "TON Payment Gateway"
    }


# =========================================================
# BSC PRICE ORACLE
# =========================================================
bsc_rpc = "https://bsc-dataseed.binance.org/"

web3 = Web3(
    Web3.HTTPProvider(
        bsc_rpc,
        request_kwargs={"timeout": 15}
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
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"}
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
        raise Exception("Invalid price")

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
# TRACK TX
# =========================================================
async def track_tx(client, wallet, old_seqno, timeout=150):

    start = time.time()

    while True:

        try:

            # =============================================
            # UNDEPLOYED WALLET TRACKING
            # =============================================
            if old_seqno == -1:

                account_state = await client.get_account_state(
                    wallet.address
                )

                if (
                    account_state
                    and account_state.is_active
                ):

                    txs = await client.get_transactions(
                        wallet.address,
                        count=5
                    )

                    if txs:
                        return txs[0].cell.hash.hex()

                await asyncio.sleep(3)

                continue

            # =============================================
            # NORMAL WALLET TRACKING
            # =============================================
            seqno = await wallet.get_seqno()

            if seqno > old_seqno:

                txs = await client.get_transactions(
                    wallet.address,
                    count=10
                )

                if txs:
                    return txs[0].cell.hash.hex()

        except Exception as e:

            print("Track tx error:", e)

        if time.time() - start > timeout:
            return None

        await asyncio.sleep(3)


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

    amount_ton = float(req.amount_ton)

    amount_usd = amount_ton * ton_price

    required = int(amount_ton * 1e9)

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
    # DEPLOYMENT FEE
    # =====================================================
    deploy_fee = int(0.05 * 1e9)

    # =====================================================
    # CHECK DEPLOY STATUS
    # =====================================================
    try:

        try:

            old_seqno = await wallet.get_seqno()

            wallet_active = True

            state_init = None

        except Exception:

            old_seqno = -1

            wallet_active = False

            state_init = wallet.state_init

    except Exception as e:

        return response(
            False,
            "Wallet prepare failed",
            {
                "error": str(e)
            },
            502
        )

    # =====================================================
    # REQUIRED TOTAL
    # =====================================================
    required_total = required

    if not wallet_active:
        required_total += deploy_fee

    # =====================================================
    # INSUFFICIENT BALANCE
    # =====================================================
    if before_balance < required_total:

        return response(
            False,
            "Insufficient balance",
            {
                "wallet": wallet_addr,

                "balance_ton": round(
                    balance_ton,
                    9
                ),

                "required_ton": round(
                    required_total / 1e9,
                    9
                ),

                "required_usd": round(
                    amount_usd,
                    6
                ),

                "deploy_required": (
                    not wallet_active
                ),

                "deploy_fee_ton": round(
                    deploy_fee / 1e9,
                    9
                ),

                "ton_price_usd": round(
                    ton_price,
                    6
                )
            },
            402
        )

    # =====================================================
    # LOCKED TRANSFER
    # =====================================================
    async with wallet_lock:

        # =================================================
        # SEND TRANSACTION
        # =================================================
        try:

            await wallet.raw_transfer(

                msgs=[
                    wallet.create_wallet_internal_message(
                        destination=req.to_address,
                        value=required,
                        body=body
                    )
                ],

                state_init=state_init
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
        # TRACK TRANSACTION
        # =================================================
        txid = await track_tx(
            client,
            wallet,
            old_seqno,
            timeout=150
        )

    # =====================================================
    # FINAL BALANCE
    # =====================================================
    try:

        after_balance = await wallet.get_balance()

    except Exception as e:

        print("Balance fetch after tx failed:", e)

        after_balance = before_balance - required

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

            "wallet_deployed": (
                not wallet_active
            ),

            "amount_ton": round(
                amount_ton,
                9
            ),

            "amount_usd": round(
                amount_usd,
                6
            ),

            "before_balance_ton": round(
                balance_ton,
                9
            ),

            "before_balance_usd": round(
                balance_ton * ton_price,
                6
            ),

            "after_balance_ton": round(
                after_balance_ton,
                9
            ),

            "after_balance_usd": round(
                after_balance_ton * ton_price,
                6
            ),

            "fee_ton": round(
                fee_ton,
                9
            ),

            "fee_usd": round(
                fee_usd,
                6
            ),

            "ton_price_usd": round(
                ton_price,
                6
            )
        }
    )


# =========================================================
# API ENDPOINT
# =========================================================
@app.post("/send")
async def send(req: SendRequest):

    return await process_payment(req)
```
