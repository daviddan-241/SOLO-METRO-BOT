"""On-chain metadata: RPCs, routers, explorers, DexScreener ids."""

import os

NATIVE_ZERO = "0x0000000000000000000000000000000000000000"
SOL_NATIVE = "So11111111111111111111111111111111111111112"

# DexScreener chainId -> our code
DEX_TO_CHAIN = {
    "solana": "SOL",
    "ethereum": "ETH",
    "bsc": "BSC",
    "base": "BASE",
    "arbitrum": "ARB",
    "avalanche": "AVAX",
    "sonic": "SONIC",
    "monad": "MONAD",
    "hyperevm": "HYPE",
    "tron": "TRX",
    "ton": "TON",
    "toncoin": "TON",
}

CHAIN_META = {
    "ETH": {
        "chain_id": 1,
        "lifi": 1,
        "dex": "ethereum",
        "decimals": 18,
        "explorer": "https://etherscan.io",
        "rpcs": [
            os.getenv("RPC_ETH", ""),
            "https://ethereum-rpc.publicnode.com",
            "https://eth.llamarpc.com",
            "https://rpc.ankr.com/eth",
        ],
        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "flashbots": "https://rpc.flashbots.net",
        "goplus": "1",
    },
    "BSC": {
        "chain_id": 56,
        "lifi": 56,
        "dex": "bsc",
        "decimals": 18,
        "explorer": "https://bscscan.com",
        "rpcs": [
            os.getenv("RPC_BSC", ""),
            "https://bsc-rpc.publicnode.com",
            "https://bsc-dataseed.binance.org",
            "https://rpc.ankr.com/bsc",
        ],
        "router": "0x10ED43C718714eb63d5aA57B78B54704E256024E",
        "weth": "0xBB4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "goplus": "56",
    },
    "BASE": {
        "chain_id": 8453,
        "lifi": 8453,
        "dex": "base",
        "decimals": 18,
        "explorer": "https://basescan.org",
        "rpcs": [
            os.getenv("RPC_BASE", ""),
            "https://base-rpc.publicnode.com",
            "https://mainnet.base.org",
        ],
        "router": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        "weth": "0x4200000000000000000000000000000000000006",
        "goplus": "8453",
    },
    "ARB": {
        "chain_id": 42161,
        "lifi": 42161,
        "dex": "arbitrum",
        "decimals": 18,
        "explorer": "https://arbiscan.io",
        "rpcs": [
            os.getenv("RPC_ARB", ""),
            "https://arbitrum-one-rpc.publicnode.com",
            "https://arb1.arbitrum.io/rpc",
        ],
        "router": "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
        "weth": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "goplus": "42161",
    },
    "AVAX": {
        "chain_id": 43114,
        "lifi": 43114,
        "dex": "avalanche",
        "decimals": 18,
        "explorer": "https://snowtrace.io",
        "rpcs": [
            os.getenv("RPC_AVAX", ""),
            "https://avalanche-c-chain-rpc.publicnode.com",
            "https://api.avax.network/ext/bc/C/rpc",
        ],
        "router": "0x60aE616a2155Ee3d9A68541Ba4544862310933d4",
        "weth": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
        "goplus": "43114",
    },
    "SONIC": {
        "chain_id": 146,
        "lifi": 146,
        "dex": "sonic",
        "decimals": 18,
        "explorer": "https://sonicscan.org",
        "rpcs": [
            os.getenv("RPC_SONIC", ""),
            "https://rpc.soniclabs.com",
        ],
        "router": "",
        "weth": "0x039e2fB66102314Ce7b64Ce5Ce3E5183bc94aD38",
        "goplus": "",
    },
    "MONAD": {
        "chain_id": 143,
        "lifi": 143,
        "dex": "monad",
        "decimals": 18,
        "explorer": "https://monadvision.com",
        "rpcs": [os.getenv("RPC_MONAD", ""), "https://rpc.monad.xyz"],
        "router": "0x4B2ab38DBF28D31D467aA8993f6c2585981D6804",
        "weth": "",
        "goplus": "",
    },
    "HYPE": {
        "chain_id": 999,
        "lifi": 999,
        "dex": "hyperevm",
        "decimals": 18,
        "explorer": "https://purrsec.com",
        "rpcs": [os.getenv("RPC_HYPE", ""), "https://rpc.hyperliquid.xyz/evm"],
        "router": "",
        "weth": "",
        "goplus": "",
    },
    "HOOD": {
        "chain_id": 0,
        "lifi": 0,
        "dex": "robinhood",
        "decimals": 18,
        "explorer": "",
        "rpcs": [os.getenv("RPC_HOOD", "")],
        "router": "0x8cFe327CEc66d1C090Dd72bd0FF11d690C33a2Eb",
        "weth": "",
        "goplus": "",
    },
    "SOL": {
        "chain_id": 0,
        "lifi": 1151111081099710,
        "dex": "solana",
        "decimals": 9,
        "explorer": "https://solscan.io",
        "rpcs": [
            os.getenv("RPC_SOL", ""),
            "https://api.mainnet-beta.solana.com",
            "https://solana-rpc.publicnode.com",
        ],
        "router": "",
        "weth": SOL_NATIVE,
        "goplus": "",
    },
    "TRX": {
        "chain_id": 0,
        "lifi": 0,
        "dex": "tron",
        "decimals": 6,
        "explorer": "https://tronscan.org/#",
        "rpcs": [],
        "router": "",
        "weth": "",
        "goplus": "",
    },
    "TON": {
        "chain_id": 0,
        "lifi": 0,
        "dex": "ton",
        "decimals": 9,
        "explorer": "https://tonviewer.com",
        "rpcs": [],
        "router": "",
        "weth": "",
        "goplus": "",
    },
}


def rpcs(chain: str) -> list[str]:
    return [u for u in CHAIN_META.get(chain, {}).get("rpcs", []) if u]


def explorer_tx(chain: str, txid: str) -> str:
    base = CHAIN_META.get(chain, {}).get("explorer") or ""
    if not base:
        return txid
    if chain == "SOL":
        return f"{base}/tx/{txid}"
    if chain == "TON":
        return f"{base}/{txid}"
    if chain == "TRX":
        return f"{base}/transaction/{txid}"
    return f"{base}/tx/{txid}"


def explorer_addr(chain: str, addr: str) -> str:
    base = CHAIN_META.get(chain, {}).get("explorer") or ""
    if not base:
        return addr
    if chain == "SOL":
        return f"{base}/account/{addr}"
    return f"{base}/address/{addr}"
