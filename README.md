# Imprint

**写真真正性認証プラットフォーム** — 保険・不動産・法務向けに、写真が本物かつ無加工であることを技術的に証明するB2B SaaS。

- **サービスサイト**: https://imprint-digital.jp
- **API / ダッシュボード**: https://api.imprint-digital.jp
- **API ドキュメント**: https://api.imprint-digital.jp/docs

---

## 機能

| 機能 | 概要 |
|------|------|
| EXIFメタデータ解析 | カメラ・GPS・撮影日時・ソフトウェア情報を完全記録 |
| ELA（誤差水準解析） | JPEG再圧縮パターンで加工箇所を検出 |
| AI生成画像検出 | HuggingFace モデルで AI 生成写真を識別 |
| 真正性スコア算出 | 0〜100点の減点方式スコア |
| RFC 3161 タイムスタンプ | FreeTSA / DigiCert / Sectigo 対応 |
| ブロックチェーン記録 | Polygon に写真ハッシュを永久保存 |
| PDF証明書発行 | A4証明書を即時生成・ダウンロード |
| APIキー認証 | `X-API-Key` ヘッダー認証（`imp_xxx` 形式） |

---

## エンドポイント

| Method | Path | 認証 | 説明 |
|--------|------|------|------|
| GET | `/` | なし | ダッシュボード UI |
| GET | `/health` | なし | ヘルスチェック |
| POST | `/verify` | APIキー | 完全検証（EXIF + ELA + AI + スコア） |
| POST | `/certificate` | APIキー | 検証 + PDF証明書ダウンロード |
| POST | `/hash` | APIキー | SHA-256ハッシュのみ（軽量版） |
| POST | `/admin/keys` | 管理者キー | APIキー発行 |
| GET | `/admin/keys` | 管理者キー | APIキー一覧 |
| DELETE | `/admin/keys/{id}` | 管理者キー | APIキー無効化 |
| POST | `/blockchain/register` | APIキー | ハッシュを Polygon に記録 |
| GET | `/blockchain/status/{hash}` | APIキー | オンチェーン登録確認 |
| POST | `/timestamp/request` | APIキー | RFC 3161 タイムスタンプ取得 |
| GET | `/timestamp/verify/{hash}` | APIキー | タイムスタンプ検証 |
| GET | `/timestamp/providers` | APIキー | 利用可能 TSA 一覧 |

---

## ローカル開発

```powershell
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
# → http://localhost:8000
```

### 環境変数（`.env` に記載）

```env
IMPRINT_ADMIN_KEY=       # 管理エンドポイント保護キー
IMPRINT_DB_PATH=         # DBファイルパス（デフォルト: imprint.db）
API_KEY=                 # ダッシュボード UI 用 APIキー
POLYGON_RPC_URL=         # Polygon RPC エンドポイント
POLYGON_PRIVATE_KEY=     # ウォレット秘密鍵
IMPRINT_CONTRACT_ADDRESS= # デプロイ済みコントラクトアドレス
POLYGON_CHAIN_ID=        # 80002=Amoy testnet / 137=Mainnet
HUGGINGFACE_API_KEY=     # HuggingFace API キー（省略時はローカル解析）
TSA_URL=                 # タイムスタンプ局 URL（省略時: https://freetsa.org/tsr）
ALLOWED_ORIGINS=         # CORS 許可オリジン（カンマ区切り）
```

---

## 真正性スコア（減点方式）

| 要因 | 減点 |
|------|------|
| EXIFデータなし | -25 |
| 加工ソフト検出（Photoshop等） | -20 |
| カメラ情報なし | -5 |
| 撮影日時なし | -5 |
| ELA：要注意 | -25 |
| ELA：加工検出 | -45 |
| ファイルサイズ小（<10KB） | -5 |
| AI生成画像（HF API使用時） | -50 |
| AI生成の疑い（HF API使用時） | -30 |
| RFC 3161 タイムスタンプ済み | +10 |

---

## デプロイ構成

| コンポーネント | サービス |
|------------|---------|
| マーケティングサイト | Cloudflare Pages（`website/` フォルダ） |
| API サーバー | Render |
| DNS | Cloudflare（imprint-digital.jp） |
| ブロックチェーン | Polygon Amoy Testnet（Chain ID: 80002） |

---

## ライセンス

Proprietary — All rights reserved.
