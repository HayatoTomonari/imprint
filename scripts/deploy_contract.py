"""
ImprintRegistry コントラクトのデプロイスクリプト

使い方:
  pip install py-solc-x
  python scripts/deploy_contract.py

必要な環境変数:
  POLYGON_RPC_URL       RPC エンドポイント
  POLYGON_PRIVATE_KEY   デプロイに使うウォレットの秘密鍵
  POLYGON_CHAIN_ID      (任意) デフォルト 80002 = Amoy testnet / 137 = mainnet
"""

import os
import sys
from pathlib import Path

try:
    from solcx import compile_source, install_solc
except ImportError:
    print("pip install py-solc-x が必要です")
    sys.exit(1)

from web3 import Web3

RPC_URL     = os.environ["POLYGON_RPC_URL"]
PRIVATE_KEY = os.environ["POLYGON_PRIVATE_KEY"]
CHAIN_ID    = int(os.getenv("POLYGON_CHAIN_ID", "80002"))

SOL_PATH = Path(__file__).parent.parent / "contracts" / "ImprintRegistry.sol"

EXPLORER_BASE = (
    "https://polygonscan.com" if CHAIN_ID == 137 else "https://amoy.polygonscan.com"
)


def _build_gas_params(w3: Web3) -> dict:
    """EIP-1559 対応なら maxFeePerGas/maxPriorityFeePerGas、非対応なら legacy gasPrice。"""
    try:
        latest = w3.eth.get_block("latest")
        if latest.get("baseFeePerGas") is not None:
            base_fee = latest["baseFeePerGas"]
            try:
                priority_fee = w3.eth.max_priority_fee
            except Exception:
                priority_fee = w3.to_wei(30, "gwei")
            max_fee = base_fee * 2 + priority_fee
            print(f"  ガス: EIP-1559  baseFee={w3.from_wei(base_fee,'gwei'):.2f} Gwei  "
                  f"maxFee={w3.from_wei(max_fee,'gwei'):.2f} Gwei")
            return {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee}
    except Exception:
        pass
    gas_price = w3.eth.gas_price
    print(f"  ガス: Legacy  gasPrice={w3.from_wei(gas_price,'gwei'):.2f} Gwei")
    return {"gasPrice": gas_price}


def main():
    network = "Polygon Mainnet" if CHAIN_ID == 137 else "Polygon Amoy Testnet"
    print(f"ネットワーク: {network} (chainId={CHAIN_ID})")

    if CHAIN_ID == 137:
        print()
        print("⚠️  Polygon Mainnet へのデプロイです。実際の MATIC が消費されます。")
        ans = input("続行しますか？ [y/N]: ").strip().lower()
        if ans != "y":
            print("中断しました")
            sys.exit(0)
        print()

    print("Solidity コンパイラをインストール中...")
    install_solc("0.8.20", show_progress=True)

    source = SOL_PATH.read_text(encoding="utf-8")
    print("コンパイル中...")
    compiled = compile_source(
        source,
        output_values=["abi", "bin"],
        solc_version="0.8.20",
    )
    contract_interface = compiled["<stdin>:ImprintRegistry"]
    abi      = contract_interface["abi"]
    bytecode = contract_interface["bin"]
    print(f"  バイトコード: {len(bytecode) // 2} bytes")

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("ノードへの接続に失敗しました")
        sys.exit(1)

    pk = PRIVATE_KEY if PRIVATE_KEY.startswith("0x") else f"0x{PRIVATE_KEY}"
    account = w3.eth.account.from_key(pk)
    balance = w3.eth.get_balance(account.address)
    print(f"デプロイアドレス: {account.address}")
    print(f"残高: {w3.from_wei(balance, 'ether'):.6f} MATIC")

    if balance == 0:
        faucet = (
            "https://faucet.polygon.technology/"
            if CHAIN_ID == 80002
            else "取引所から送金してください"
        )
        print(f"残高が0です。MATIC を補充してください: {faucet}")
        sys.exit(1)

    registry = w3.eth.contract(abi=abi, bytecode=bytecode)

    # ガスを動的に見積もり（20% バッファ付き）
    gas_estimate = registry.constructor().estimate_gas({"from": account.address})
    gas_limit = int(gas_estimate * 1.2)
    print(f"  ガス見積もり: {gas_estimate:,} → 制限: {gas_limit:,}")

    nonce = w3.eth.get_transaction_count(account.address)
    tx = registry.constructor().build_transaction({
        "chainId": CHAIN_ID,
        "from": account.address,
        "nonce": nonce,
        "gas": gas_limit,
        **_build_gas_params(w3),
    })
    signed = account.sign_transaction(tx)
    print("デプロイ中...")
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = tx_hash.hex()
    print(f"  TX: {EXPLORER_BASE}/tx/{tx_hex}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)

    if receipt["status"] == 0:
        print(f"❌ トランザクションが失敗しました: {EXPLORER_BASE}/tx/{tx_hex}")
        sys.exit(1)

    contract_addr = receipt["contractAddress"]
    print(f"\n✅ デプロイ完了！")
    print(f"   コントラクト: {contract_addr}")
    print(f"   Polygonscan:  {EXPLORER_BASE}/address/{contract_addr}")
    print(f"\n以下を .env に追記/更新してください:")
    print(f"IMPRINT_CONTRACT_ADDRESS={contract_addr}")
    print(f"POLYGON_RPC_URL={RPC_URL}")
    print(f"POLYGON_CHAIN_ID={CHAIN_ID}")


if __name__ == "__main__":
    main()
