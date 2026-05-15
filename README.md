# Imprint API

写真の真正性を技術的に担保するバックエンドAPI。

## セットアップ

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## エンドポイント

| Method | Path | 説明 |
|--------|------|------|
| GET | `/` | ヘルスチェック |
| POST | `/verify` | 写真の完全検証（EXIF + ELA + スコア） |
| POST | `/hash` | SHA-256ハッシュのみ取得（軽量版） |

## `/verify` レスポンス例

```json
{
  "verified_at": "2025-01-01T00:00:00+00:00",
  "file": {
    "filename": "photo.jpg",
    "size_bytes": 1234567,
    "width": 3024,
    "height": 4032
  },
  "hash": {
    "algorithm": "SHA-256",
    "value": "abc123...",
    "note": "このハッシュ値をブロックチェーンに記録することで改ざん検知が可能"
  },
  "exif": {
    "has_exif": true,
    "camera_make": "Apple",
    "camera_model": "iPhone 15 Pro",
    "datetime_original": "2025:01:01 12:00:00",
    "gps_latitude": 35.681236,
    "gps_longitude": 139.767125,
    "software": null,
    "warnings": []
  },
  "ela": {
    "ela_max_diff": 12.3,
    "ela_mean_diff": 0.42,
    "ela_suspicious_ratio": 0.003,
    "ela_verdict": "clean"
  },
  "authenticity": {
    "score": 95.0,
    "verdict": "high",
    "verdict_label": "高（改ざんの可能性は低い）",
    "deductions": [],
    "details": ["ELA解析：加工痕跡なし"]
  }
}
```

## 真正性スコアの基準

| スコア | 判定 | 意味 |
|--------|------|------|
| 80〜100 | high | 改ざんの可能性は低い |
| 50〜79 | medium | 要確認 |
| 0〜49 | low | 改ざんの疑いあり |

## スコア減点ロジック

| 要因 | 減点 |
|------|------|
| EXIFデータなし | -25 |
| 編集ソフト検出（Photoshop等） | -20 |
| カメラ情報なし | -5 |
| 撮影日時なし | -5 |
| ELA：要注意 | -25 |
| ELA：加工検出 | -45 |
| ファイルサイズ小（<10KB） | -5 |

## 今後の拡張予定

- [ ] ブロックチェーン（Polygon）へのハッシュ記録
- [ ] タイムスタンプ認証（RFC 3161）
- [ ] AIモデルによる生成画像検出
- [ ] 証明書PDF発行
- [ ] WebhookによるB2B連携
