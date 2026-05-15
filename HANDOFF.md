# Imprint — Claude Code 引き継ぎドキュメント

## プロジェクト概要
写真の真正性を技術的に担保するB2B SaaS「Imprint」のバックエンドAPI。
保険・不動産・法務向けに、写真が本物かつ無加工であることを証明する。

## プロジェクトパス
```
C:\Users\info\PycharmProjects\Imprint\
├── app/
│   └── main.py            ← メインAPI（v0.6.0）
├── contracts/
│   └── ImprintRegistry.sol ← Solidityコントラクト
├── scripts/
│   └── deploy_contract.py  ← コントラクトデプロイスクリプト
├── .env                   ← 環境変数（gitignore済み）
├── imprint.db             ← APIキー管理DB（SQLite、gitignore済み）
├── requirements.txt
├── README.md
└── .gitignore
```

## 技術スタック
- Python + FastAPI
- Pillow（画像処理）
- piexif（EXIFメタデータ抽出）
- numpy（ELA解析）
- reportlab（PDF証明書生成）
- web3.py（Polygonブロックチェーン連携）
- python-dotenv（.env自動読み込み）

## 全エンドポイント一覧

| Method | Path | 認証 | 説明 |
|--------|------|------|------|
| GET | `/` | なし | ダッシュボードUI |
| GET | `/health` | なし | ヘルスチェック |
| POST | `/verify` | APIキー | 完全検証（EXIF + ELA + AI検出 + スコア） |
| POST | `/certificate` | APIキー | 検証＋PDF証明書ダウンロード |
| POST | `/hash` | APIキー | SHA-256ハッシュのみ（軽量版） |
| POST | `/admin/keys` | 管理者キー | APIキー発行 |
| GET | `/admin/keys` | 管理者キー | APIキー一覧 |
| DELETE | `/admin/keys/{id}` | 管理者キー | APIキー無効化 |
| POST | `/blockchain/register` | APIキー | ハッシュをPolygonに記録 |
| GET | `/blockchain/status/{hash}` | APIキー | オンチェーン登録確認 |
| POST | `/timestamp/request` | APIキー | RFC 3161 タイムスタンプを TSA に要求・DB保存 |
| GET | `/timestamp/verify/{hash}` | APIキー | 保存済みタイムスタンプトークンを検証 |

## 実装済み機能

### コア機能（v0.1〜0.2）
1. **SHA-256ハッシュ生成** — 改ざん検知の基準値
2. **EXIFメタデータ抽出** — カメラ・GPS・日時・ソフトウェア情報
3. **ソフトウェア分類**
   - `develop`（Lightroom, Capture One等）→ 減点なし
   - `edit`（Photoshop, GIMP, Facetune等）→ -20点
4. **ELA（Error Level Analysis）** — JPEG再圧縮誤差で加工箇所を検出
5. **真正性スコア算出**（0〜100点、減点方式）
6. **PDF証明書発行** — reportlabでA4証明書を生成

### APIキー認証（v0.3.0）
- SQLite（`imprint.db`）でAPIキーを永続管理
- `X-API-Key` ヘッダー認証（`imp_xxx` 形式）
- `X-Admin-Key` ヘッダーで管理エンドポイントを保護
- キーはSHA-256ハッシュのみDB保存（平文保存なし）

### ブロックチェーン記録（v0.4.0）
- ImprintRegistry.sol（Solidity 0.8.20）をPolygon Amoy testnetにデプロイ済み
- 写真ハッシュ+真正性スコアをオンチェーンに永久記録
- 冪等設計（同一ハッシュ再送信時は既登録情報を返す）

### Webフロントエンド（v0.5.0）
- `GET /` → ダッシュボードUI（Jinja2 + バニラJS）
- ドラッグ＆ドロップで写真をアップロード
- スコアゲージ（SVGアーク、緑/黄/赤）
- EXIF・ELA・ハッシュ・ブロックチェーン登録を1画面で表示
- `API_KEY`（.env）をサーバーがHTMLに埋め込み → ブラウザは自動認証

### AI生成画像検出（v0.6.0）
- HuggingFace Inference API（`umm-maybe/AI-image-detector`）で検出
- `HUGGINGFACE_API_KEY` 未設定時はローカルFFT周波数解析にフォールバック
- `/verify`・`/certificate` レスポンスに `ai_detection` フィールドを追加
- HF API使用時のみスコアに反映（AI生成: -50点、疑い: -30点）

### RFC 3161 タイムスタンプ認証（v0.7.x）
- FreeTSA.org（`https://freetsa.org/tsr`）を使用（環境変数 `TSA_URL` で変更可）
- `rfc3161ng` ライブラリ（pyasn1 ベース）でリクエスト構築・レスポンス解析
- SHA-256 ダイジェストで TSA にタイムスタンプ要求、DER トークンを SQLite に保存
- シリアル番号・TSA 認定時刻をレスポンスに含む
- 同一ハッシュへの重複要求は冪等（既存情報を返す）
- **注意**: `rfc3161ng` の `data=` 引数にはバグあり（常に SHA-1 でハッシュする）。`digest=` を使用すること
- `/timestamp/verify/{hash}`: 埋め込み TSA 証明書で署名検証（RSA/ECDSA 両対応）
  - `rfc3161ng.check_timestamp` は RSA 専用のため、自前の `_check_timestamp_rsa_or_ec()` を使用
- `/verify` レスポンスに `timestamp` フィールド追加（タイムスタンプ済みの場合）
- `/certificate` PDF に "RFC 3161 Timestamp" セクション追加（TSA URL・認定時刻・シリアル番号）

## スコアロジック（減点方式）

| 要因 | 減点 |
|------|------|
| EXIFデータなし | -25 |
| 加工ソフト検出（Photoshop等） | -20 |
| 現像ソフト（Lightroom等） | 0（情報記録のみ） |
| カメラ情報なし | -5 |
| 撮影日時なし | -5 |
| ELA要注意 | -25 |
| ELA加工検出 | -45 |
| ファイルサイズ小（<10KB） | -5 |
| AI生成画像（HF API時のみ） | -50 |
| AI生成の疑い（HF API時のみ） | -30 |

## デプロイ済みコントラクト情報

| 項目 | 値 |
|------|------|
| ネットワーク | Polygon Amoy Testnet（Chain ID: 80002） |
| コントラクトアドレス | `0xD080d787bec4DCf35e6525f23217f6c2799DA22f` |
| デプロイアドレス | `0x09300EEE2230377d0438667Fe153e8633a51e89F` |
| Polygonscan | https://amoy.polygonscan.com/address/0xD080d787bec4DCf35e6525f23217f6c2799DA22f |

## 環境変数（.envで管理）

| 変数 | 説明 |
|------|------|
| `IMPRINT_ADMIN_KEY` | 管理エンドポイント保護キー |
| `IMPRINT_DB_PATH` | DBファイルパス（デフォルト: `imprint.db`） |
| `POLYGON_RPC_URL` | RPCエンドポイント |
| `POLYGON_PRIVATE_KEY` | ウォレット秘密鍵（送信用） |
| `IMPRINT_CONTRACT_ADDRESS` | デプロイ済みコントラクトアドレス |
| `POLYGON_CHAIN_ID` | `80002`=Amoy testnet / `137`=mainnet |
| `API_KEY` | WebフロントエンドがHTMLに埋め込むAPIキー |
| `HUGGINGFACE_API_KEY` | HuggingFace APIキー（無料で取得可） |
| `TSA_URL` | タイムスタンプ局URL（デフォルト: `https://freetsa.org/tsr`） |

## 起動方法
```powershell
cd C:\Users\info\PycharmProjects\Imprint
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
# → http://localhost:8000/docs
```
.envが自動で読み込まれるため、環境変数の手動設定は不要。

---

### v0.8.0 追加実装
- **CA ルート証明書チェーン検証**: FreeTSA.org の cacert.pem を起動時にフェッチ・キャッシュし、TSA 署名証明書の直接署名を `verify_directly_issued_by()` で検証。`chain_verified` フィールドで返す
- **フロントエンドへのタイムスタンプ UI 追加**: 「RFC 3161 タイムスタンプ」カード。取得済みなら TSA 情報を表示、未取得なら「取得する」ボタン（インディゴ色）を表示
- **タイムスタンプボーナス**: タイムスタンプ済みの場合、真正性スコアに **+10点** を加算。`_query_timestamp()` を `compute_authenticity_score()` より先に呼ぶよう実行順を変更

### v0.9.0 追加実装
- **ダッシュボード PDF ダウンロード**: スコアカードに「PDF証明書をダウンロード」ボタンを追加。クライアントから `/certificate` へ fetch → Blob → `<a download>` でブラウザ保存
- **CA 中間証明書チェーン対応**: `_verify_cert_chain()` を全面書き換え
  - TST の `certificates` フィールドから全埋め込み証明書を抽出（`getComponent()` + DER encode）
  - `SignerInfo.issuerAndSerialNumber` のシリアル番号で署名証明書を特定
  - issuer 名（DER 比較 + RFC 4514 文字列フォールバック）を辿ってチェーンを組み立て
  - `verify_directly_issued_by()` で各リンクを暗号学的に検証
  - 最大 8 段の中間 CA チェーンに対応。FreeTSA.org の2段チェーンで動作確認済み

### v0.9.0 追加実装（続き）
- **Polygon Mainnet コード対応**: EIP-1559 ガス自動選択（`_build_gas_params`）— `baseFeePerGas` があれば `maxFeePerGas/maxPriorityFeePerGas`、なければ legacy `gasPrice` にフォールバック
- **`/health` 拡充**: `blockchain.configured / network / chain_id / contract / explorer` フィールドを追加
- **UI ネットワーク表示**: Jinja2 テンプレートで `network_name / chain_id` を渡し、ブロックチェーンカードにネットワークバッジ（Mainnet=緑 / Testnet=黄）を常時表示
- **`deploy_contract.py` 改善**: EIP-1559 対応・動的ガス見積もり・Mainnet 時の確認プロンプト・エクスプローラーURL表示・デプロイ失敗検出

**Mainnet デプロイ手順**（コード実装済み、以下はオペレーション作業）:
1. Polygon Mainnet 用 RPC URL を取得（Alchemy / Infura / QuickNode など）
2. ウォレットに MATIC を用意（デプロイに ~0.01 MATIC 程度）
3. `.env` を更新: `POLYGON_RPC_URL=<mainnet_rpc>`, `POLYGON_CHAIN_ID=137`
4. `python scripts/deploy_contract.py` を実行（確認プロンプトで `y`）
5. 出力された `IMPRINT_CONTRACT_ADDRESS` を `.env` に追記

### v0.9.0 追加実装（続き）
- **複数 TSA 対応**: `_TSA_PROVIDERS` dict（freetsa / digicert / sectigo）で複数 TSA を定義
  - `_fetch_ts_token(raw_bytes, tsa_url)` — tsa_url 引数化
  - `_fetch_tsa_cacert(tsa_url)` — TSA ごとに CA 証明書を管理（ca_cert_url が None の TSA はチェーン検証スキップ）
  - `GET /timestamp/providers` — 利用可能 TSA 一覧・chain_verification 対応状況を返す
  - `POST /timestamp/request` — `tsa` フォームフィールドを追加（freetsa / digicert / sectigo、省略時はデフォルト）
  - UI に TSA セレクター（ラジオボタン）を追加。タイムスタンプ未取得時のみ表示、取得済みで非表示
  - `chain_verified: null`（not checked）は digicert・sectigo（CA cert URL 未定義）、`true/false` は freetsa

### v0.9.0 追加実装（続き）
- **商用 TSA AIA チェーン検証**: `_verify_cert_chain_aia(tst)` を追加。DigiCert・Sectigo 等の `ca_cert_url` 未定義 TSA で動作
  - `_fetch_issuer_via_aia(cert)` — X.509 AIA 拡張（`caIssuers`）から中間 CA 証明書を HTTP フェッチ（PEM/DER 自動判別）
  - `_get_trusted_root_fps()` — `certifi` Mozilla バンドルから SHA-256 フィンガープリントをロードし、信頼済みルート CA セットを構築（起動時に約 120 件）
  - チェーン構築: 埋め込み証明書 → なければ AIA フェッチ → 最大 8 段、ルートで終端
  - 各リンクを `verify_directly_issued_by()` で暗号学的に検証後、ルート CA のフィンガープリントを certifi バンドルと照合
  - `/timestamp/verify/{hash}` — `ca_pem is not None` で `_verify_cert_chain()`（FreeTSA）、`None` で `_verify_cert_chain_aia()`（DigiCert/Sectigo）に分岐
  - `GET /timestamp/providers` — 全 TSA で `chain_verification: true`、`verification_method: "cacert_url" | "aia"` を返す
- **バージョン**: `0.9.0`

## 次の実装候補（優先度順）
1. **Polygon Mainnet デプロイ** — オペレーション作業（HANDOFF 内の手順を参照）
