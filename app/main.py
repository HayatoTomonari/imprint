"""
Imprint API - 写真真正性担保サービス
"""

import base64
import hashlib
import io
import json
import math
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

_BASE = Path(__file__).parent

import numpy as np
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Depends, Body, Request, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, RedirectResponse, JSONResponse
from fastapi.security import APIKeyHeader
from jose import jwt, JWTError
from passlib.context import CryptContext
import stripe as stripe_lib
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
from PIL import Image, ImageChops, ImageFilter
import piexif
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# ──────────────────────────────────────────────
# データベース & APIキー認証
# ──────────────────────────────────────────────

DB_PATH    = os.getenv("IMPRINT_DB_PATH", "imprint.db")
ADMIN_KEY  = os.getenv("IMPRINT_ADMIN_KEY", "")
SITE_URL   = os.getenv("SITE_URL", "https://api.imprint-digital.jp")

# JWT
JWT_SECRET    = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 7

# パスワードハッシュ
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Stripe
STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_BUSINESS_PRICE  = os.getenv("STRIPE_BUSINESS_PRICE_ID", "")
if STRIPE_SECRET_KEY:
    stripe_lib.api_key = STRIPE_SECRET_KEY

# プランごとの月次上限（None=無制限）
PLAN_LIMITS: dict[str, int | None] = {
    "starter":    50,
    "business":   1000,
    "enterprise": None,
}
PLAN_LABELS: dict[str, str] = {
    "starter":    "Starter（無料）",
    "business":   "Business",
    "enterprise": "Enterprise",
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    conn = _db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id                TEXT PRIMARY KEY,
            email             TEXT UNIQUE NOT NULL,
            password_hash     TEXT NOT NULL,
            plan              TEXT NOT NULL DEFAULT 'starter',
            stripe_customer_id TEXT,
            created_at        TEXT NOT NULL,
            is_active         INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monthly_usage (
            user_id    TEXT NOT NULL,
            year_month TEXT NOT NULL,
            count      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, year_month)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id          TEXT PRIMARY KEY,
            key_hash    TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            is_active   INTEGER NOT NULL DEFAULT 1,
            req_count   INTEGER NOT NULL DEFAULT 0,
            last_used   TEXT,
            user_id     TEXT REFERENCES users(id)
        )
    """)
    # 既存テーブルへの user_id カラム追加（初回のみ）
    try:
        conn.execute("ALTER TABLE api_keys ADD COLUMN user_id TEXT REFERENCES users(id)")
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timestamps (
            id           TEXT PRIMARY KEY,
            image_hash   TEXT NOT NULL UNIQUE,
            tsa_url      TEXT NOT NULL,
            token_b64    TEXT NOT NULL,
            serial_no    TEXT,
            tsa_time     TEXT NOT NULL,
            requested_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_api_key_display (
            user_id  TEXT PRIMARY KEY,
            raw_key  TEXT,
            shown    INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id             TEXT PRIMARY KEY,
            user_id        TEXT NOT NULL,
            filename       TEXT NOT NULL,
            file_size      INTEGER,
            file_hash      TEXT,
            score          REAL,
            verdict        TEXT,
            ela_verdict    TEXT,
            ai_verdict     TEXT,
            has_timestamp  INTEGER NOT NULL DEFAULT 0,
            has_blockchain INTEGER NOT NULL DEFAULT 0,
            tx_hash        TEXT,
            created_at     TEXT NOT NULL,
            UNIQUE(user_id, file_hash)
        )
    """)
    conn.commit()
    conn.close()


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ── JWT ──────────────────────────────────────────

def _create_session_token(user_id: str) -> str:
    from datetime import timedelta
    exp = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS)
    return jwt.encode({"sub": user_id, "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_session_token(token: str) -> str:
    """JWT を検証して user_id を返す。失敗時は None"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


def _get_user(conn, user_id: str):
    return conn.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()


# ── 月次利用制限 ──────────────────────────────────

def _check_and_increment_usage(conn, user_id: str, plan: str) -> None:
    limit = PLAN_LIMITS.get(plan)
    if limit is None:
        return
    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    conn.execute(
        "INSERT INTO monthly_usage (user_id, year_month, count) VALUES (?, ?, 0) "
        "ON CONFLICT(user_id, year_month) DO NOTHING",
        (user_id, ym),
    )
    row = conn.execute(
        "SELECT count FROM monthly_usage WHERE user_id = ? AND year_month = ?",
        (user_id, ym),
    ).fetchone()
    if row and row["count"] >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"月間利用制限（{limit}回）に達しました。プランをアップグレードしてください。",
        )
    conn.execute(
        "UPDATE monthly_usage SET count = count + 1 WHERE user_id = ? AND year_month = ?",
        (user_id, ym),
    )


def _get_usage(conn, user_id: str) -> int:
    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    row = conn.execute(
        "SELECT count FROM monthly_usage WHERE user_id = ? AND year_month = ?",
        (user_id, ym),
    ).fetchone()
    return row["count"] if row else 0


# ── セッション依存関係 ────────────────────────────

async def _current_user_or_none(imprint_session: str = Cookie(default=None)):
    """Cookie からユーザーを取得。未ログインなら None"""
    if not imprint_session:
        return None
    user_id = _decode_session_token(imprint_session)
    if not user_id:
        return None
    conn = _db()
    try:
        return _get_user(conn, user_id)
    finally:
        conn.close()


async def _require_user(imprint_session: str = Cookie(default=None)):
    """Cookie からユーザーを取得。未ログインなら 401"""
    user_id = _decode_session_token(imprint_session or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="ログインが必要です")
    conn = _db()
    try:
        user = _get_user(conn, user_id)
        if not user:
            raise HTTPException(status_code=401, detail="ユーザーが見つかりません")
        return user
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db()
    yield


# ── セキュリティスキーム ──

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def require_api_key(x_api_key: str = Depends(_api_key_header)) -> str:
    """APIキーを検証し、key_id を返す（利用制限なし）"""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key ヘッダーが必要です")
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id FROM api_keys WHERE key_hash = ? AND is_active = 1",
            (_hash_key(x_api_key),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="APIキーが無効または無効化されています")
        conn.execute(
            "UPDATE api_keys SET req_count = req_count + 1, last_used = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), row["id"]),
        )
        conn.commit()
        return row["id"]
    finally:
        conn.close()


async def require_api_key_limited(x_api_key: str = Depends(_api_key_header)) -> str:
    """APIキーを検証し、月次利用制限をチェックして key_id を返す"""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key ヘッダーが必要です")
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, user_id FROM api_keys WHERE key_hash = ? AND is_active = 1",
            (_hash_key(x_api_key),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="APIキーが無効または無効化されています")
        key_id, user_id = row["id"], row["user_id"]
        if user_id:
            user = conn.execute("SELECT plan FROM users WHERE id = ?", (user_id,)).fetchone()
            if user:
                _check_and_increment_usage(conn, user_id, user["plan"])
        conn.execute(
            "UPDATE api_keys SET req_count = req_count + 1, last_used = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), key_id),
        )
        conn.commit()
        return key_id
    finally:
        conn.close()


def require_admin(x_admin_key: str = Depends(_admin_key_header)) -> None:
    """管理者キーを検証する"""
    if not ADMIN_KEY:
        raise HTTPException(
            status_code=503,
            detail="管理機能が設定されていません（環境変数 IMPRINT_ADMIN_KEY を設定してください）",
        )
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="管理者キーが無効です")


app = FastAPI(
    title="Imprint API",
    description="写真の真正性を技術的に担保するAPI",
    version="0.9.0",
    lifespan=lifespan,
)

_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "https://imprint-digital.jp,https://imprint-dje.pages.dev").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")
_templates = Jinja2Templates(directory=str(_BASE / "templates"))
_WEB_API_KEY = os.getenv("API_KEY", "")

# ──────────────────────────────────────────────
# ソフトウェア分類定義
# ──────────────────────────────────────────────

# 現像ツール：RAW現像・色調整のみ → 減点なし、情報として記録
DEVELOP_TOOLS = [
    "lightroom", "camera raw", "capture one", "darktable",
    "rawtherapee", "silkypix", "luminar", "on1",
]

# 加工ツール：合成・切り抜き・修正が可能 → 減点あり
EDIT_TOOLS = [
    "photoshop", "gimp", "affinity photo", "affinity",
    "snapseed", "facetune", "pixelmator", "meitu",
    "picsart", "vsco", "adobe express",
]


# ──────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────

def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_exif(image: Image.Image) -> dict:
    """EXIFメタデータを抽出して構造化する"""
    result = {
        "has_exif": False,
        "camera_make": None,
        "camera_model": None,
        "datetime_original": None,
        "gps_latitude": None,
        "gps_longitude": None,
        "software": None,
        "flash": None,
        "focal_length": None,
        "iso": None,
        "warnings": [],
    }

    try:
        exif_bytes = image.info.get("exif")
        if not exif_bytes:
            result["warnings"].append("EXIFデータなし（スクリーンショット・加工済み画像の可能性）")
            return result

        exif_dict = piexif.load(exif_bytes)
        result["has_exif"] = True

        ifd0 = exif_dict.get("0th", {})
        exif_ifd = exif_dict.get("Exif", {})
        gps_ifd = exif_dict.get("GPS", {})

        # カメラ情報
        if piexif.ImageIFD.Make in ifd0:
            result["camera_make"] = ifd0[piexif.ImageIFD.Make].decode("utf-8", errors="ignore").strip("\x00")
        if piexif.ImageIFD.Model in ifd0:
            result["camera_model"] = ifd0[piexif.ImageIFD.Model].decode("utf-8", errors="ignore").strip("\x00")
        if piexif.ImageIFD.Software in ifd0:
            sw = ifd0[piexif.ImageIFD.Software].decode("utf-8", errors="ignore").strip("\x00")
            result["software"] = sw
            sw_lower = sw.lower()
            if any(k in sw_lower for k in EDIT_TOOLS):
                result["software_category"] = "edit"
                result["warnings"].append(f"加工ソフト検出: {sw}")
            elif any(k in sw_lower for k in DEVELOP_TOOLS):
                result["software_category"] = "develop"
                result["warnings"].append(f"現像ソフト検出（スコアに影響しません）: {sw}")
            else:
                result["software_category"] = "unknown"

        # 撮影日時
        if piexif.ExifIFD.DateTimeOriginal in exif_ifd:
            dt_str = exif_ifd[piexif.ExifIFD.DateTimeOriginal].decode("utf-8", errors="ignore")
            result["datetime_original"] = dt_str

        # ISO
        if piexif.ExifIFD.ISOSpeedRatings in exif_ifd:
            result["iso"] = exif_ifd[piexif.ExifIFD.ISOSpeedRatings]

        # 焦点距離
        if piexif.ExifIFD.FocalLength in exif_ifd:
            fl = exif_ifd[piexif.ExifIFD.FocalLength]
            if isinstance(fl, tuple) and fl[1] != 0:
                result["focal_length"] = round(fl[0] / fl[1], 1)

        # GPS
        def dms_to_decimal(dms, ref):
            d = dms[0][0] / dms[0][1]
            m = dms[1][0] / dms[1][1]
            s = dms[2][0] / dms[2][1]
            val = d + m / 60 + s / 3600
            if ref in (b"S", b"W"):
                val = -val
            return round(val, 6)

        if piexif.GPSIFD.GPSLatitude in gps_ifd and piexif.GPSIFD.GPSLatitudeRef in gps_ifd:
            result["gps_latitude"] = dms_to_decimal(
                gps_ifd[piexif.GPSIFD.GPSLatitude],
                gps_ifd[piexif.GPSIFD.GPSLatitudeRef]
            )
        if piexif.GPSIFD.GPSLongitude in gps_ifd and piexif.GPSIFD.GPSLongitudeRef in gps_ifd:
            result["gps_longitude"] = dms_to_decimal(
                gps_ifd[piexif.GPSIFD.GPSLongitude],
                gps_ifd[piexif.GPSIFD.GPSLongitudeRef]
            )

    except Exception as e:
        result["warnings"].append(f"EXIF解析エラー: {str(e)}")

    return result


def ela_analysis(image: Image.Image, quality: int = 90) -> dict:
    """
    Error Level Analysis（ELA）
    - オリジナルをJPEG再圧縮し、差分を算出
    - 編集部分は再圧縮誤差が大きくなる性質を利用
    """
    result = {
        "ela_max_diff": 0.0,
        "ela_mean_diff": 0.0,
        "ela_suspicious_ratio": 0.0,
        "ela_verdict": "unknown",
    }

    try:
        # RGB変換
        img_rgb = image.convert("RGB")

        # 再圧縮
        buffer = io.BytesIO()
        img_rgb.save(buffer, format="JPEG", quality=quality)
        buffer.seek(0)
        recompressed = Image.open(buffer).convert("RGB")

        # 差分計算
        diff = ImageChops.difference(img_rgb, recompressed)
        diff_array = np.array(diff).astype(np.float32)

        ela_max = float(diff_array.max())
        ela_mean = float(diff_array.mean())

        # 増幅してピクセル単位の可視化用スケール
        scale = 10.0
        ela_scaled = np.clip(diff_array * scale, 0, 255)

        # 疑わしいピクセルの割合（ELA値が閾値以上）
        threshold = 15.0
        suspicious_pixels = np.sum(diff_array > threshold)
        total_pixels = diff_array.shape[0] * diff_array.shape[1]
        suspicious_ratio = float(suspicious_pixels / total_pixels)

        result["ela_max_diff"] = round(ela_max, 2)
        result["ela_mean_diff"] = round(ela_mean, 4)
        result["ela_suspicious_ratio"] = round(suspicious_ratio, 4)

        # 判定
        if ela_mean < 1.0 and suspicious_ratio < 0.01:
            result["ela_verdict"] = "clean"
        elif ela_mean < 3.0 and suspicious_ratio < 0.05:
            result["ela_verdict"] = "suspicious"
        else:
            result["ela_verdict"] = "likely_edited"

    except Exception as e:
        result["ela_verdict"] = "error"
        result["ela_error"] = str(e)

    return result


# ──────────────────────────────────────────────
# AI生成画像検出
# ──────────────────────────────────────────────

HF_API_KEY    = os.getenv("HUGGINGFACE_API_KEY", "")
_HF_MODEL_URL = "https://router.huggingface.co/hf-inference/models/umm-maybe/AI-image-detector"

# 対応 TSA プロバイダー定義
_TSA_PROVIDERS: dict[str, dict] = {
    "freetsa": {
        "name": "FreeTSA.org",
        "url": "https://freetsa.org/tsr",
        "description": "オープンソース・無料の認定タイムスタンプ局",
        "ca_cert_url": "https://freetsa.org/files/cacert.pem",
    },
    "digicert": {
        "name": "DigiCert",
        "url": "http://timestamp.digicert.com",
        "description": "世界最大の商用認証局。主要 OS・ブラウザに標準搭載",
        "ca_cert_url": None,
    },
    "sectigo": {
        "name": "Sectigo",
        "url": "http://timestamp.sectigo.com",
        "description": "主要な商用認証局（旧 Comodo）",
        "ca_cert_url": None,
    },
}

# 環境変数 TSA_URL が _TSA_PROVIDERS の url に一致すれば、そのキーをデフォルトにする
_DEFAULT_TSA_KEY: str = next(
    (k for k, v in _TSA_PROVIDERS.items() if v["url"] == os.getenv("TSA_URL", "")),
    "freetsa",
)

_CACERT_CACHE: dict[str, bytes] = {}


async def _fetch_tsa_cacert(tsa_url: str) -> bytes | None:
    """指定 TSA に対応する CA 証明書を取得する（メモリキャッシュあり）。
    ca_cert_url が未定義の TSA は None を返す（チェーン検証はスキップ）。
    """
    provider = next((p for p in _TSA_PROVIDERS.values() if p["url"] == tsa_url), None)
    ca_cert_url = provider["ca_cert_url"] if provider else None
    if not ca_cert_url:
        return None
    if ca_cert_url in _CACERT_CACHE:
        return _CACERT_CACHE[ca_cert_url]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(ca_cert_url)
            if resp.status_code == 200:
                _CACERT_CACHE[ca_cert_url] = resp.content
                return resp.content
    except Exception:
        pass
    return None


# certifi ルート CA バンドルの SHA-256 フィンガープリントセット（初回呼び出し時に構築）
_TRUSTED_ROOT_FPS: set[bytes] | None = None


def _get_trusted_root_fps() -> set[bytes]:
    """certifi のルート CA バンドルを読み込み、フィンガープリントの set を返す（メモリキャッシュ）。"""
    global _TRUSTED_ROOT_FPS
    if _TRUSTED_ROOT_FPS is not None:
        return _TRUSTED_ROOT_FPS
    import certifi, re
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    pem_data = open(certifi.where(), "rb").read()
    fps: set[bytes] = set()
    for pem in re.findall(
        b"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem_data, re.DOTALL
    ):
        try:
            fps.add(x509.load_pem_x509_certificate(pem).fingerprint(hashes.SHA256()))
        except Exception:
            pass
    _TRUSTED_ROOT_FPS = fps
    return fps


async def _fetch_issuer_via_aia(cert) -> "object | None":
    """AIA 拡張の caIssuers URL から発行元証明書を非同期フェッチする。"""
    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID, AuthorityInformationAccessOID
    try:
        aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS)
    except x509.ExtensionNotFound:
        return None
    for desc in aia.value:
        if desc.access_method == AuthorityInformationAccessOID.CA_ISSUERS:
            url = desc.access_location.value
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        raw = resp.content
                        return (
                            x509.load_pem_x509_certificate(raw) if b"-----BEGIN" in raw
                            else x509.load_der_x509_certificate(raw)
                        )
            except Exception:
                pass
    return None


async def _verify_cert_chain_aia(tst) -> None:
    """
    AIA 拡張を利用して証明書チェーンを動的に構築し、certifi のルート CA バンドルで検証する。
    ca_cert_url が未定義の TSA（DigiCert, Sectigo など）用。

    手順:
    1. TST の embedded certs から署名証明書を特定
    2. issuer 名を辿り、見つからなければ AIA caIssuers URL からフェッチ
    3. 自己署名（ルート CA）に到達したらチェーン完成
    4. verify_directly_issued_by() で各リンクを検証
    5. ルート CA が certifi バンドルに含まれるか確認
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from pyasn1.codec.der import encoder as asn1_enc

    signed_data = tst.content

    # ── 埋め込み証明書を全件抽出 ──────────────────────────────────────────────
    embedded: list[x509.Certificate] = []
    certs_field = signed_data.getComponentByName("certificates")
    if certs_field is not None and certs_field.hasValue():
        for cert_choice in certs_field:
            for get_der in (
                lambda c: asn1_enc.encode(c.getComponent()),
                lambda c: asn1_enc.encode(c),
            ):
                try:
                    embedded.append(x509.load_der_x509_certificate(get_der(cert_choice)))
                    break
                except Exception:
                    pass

    if not embedded:
        raise ValueError("証明書チェーンが TST に含まれていません")

    # ── 署名証明書を特定 ──────────────────────────────────────────────────────
    signer_info = signed_data["signerInfos"][0]
    try:
        signer_serial = int(
            signer_info["signerIdentifier"]["issuerAndSerialNumber"]["serialNumber"]
        )
        signer_cert = next(
            (c for c in embedded if c.serial_number == signer_serial), embedded[0]
        )
    except Exception:
        signer_cert = embedded[0]

    def _names_equal(a: x509.Name, b: x509.Name) -> bool:
        return a == b or a.rfc4514_string() == b.rfc4514_string()

    # ── チェーンを構築（embedded → AIA フェッチの順で発行者を探す） ─────────
    chain: list[x509.Certificate] = [signer_cert]
    seen_serials: set[int] = {signer_cert.serial_number}
    current = signer_cert

    for _ in range(8):
        if _names_equal(current.issuer, current.subject):
            break  # 自己署名 = ルート CA に到達

        issuer = next(
            (c for c in embedded
             if c.serial_number not in seen_serials and _names_equal(c.subject, current.issuer)),
            None,
        )
        if issuer is None:
            issuer = await _fetch_issuer_via_aia(current)
        if issuer is None:
            raise ValueError(
                f"発行元証明書を取得できません: {current.issuer.rfc4514_string()}"
            )
        chain.append(issuer)
        seen_serials.add(issuer.serial_number)
        current = issuer

    # ── 各リンクを暗号学的に検証 ─────────────────────────────────────────────
    for i in range(len(chain) - 1):
        chain[i].verify_directly_issued_by(chain[i + 1])

    # ── ルート CA を certifi バンドルで確認 ──────────────────────────────────
    root = chain[-1]
    if not _names_equal(root.issuer, root.subject):
        raise ValueError("チェーンがルート CA に到達しませんでした")
    if root.fingerprint(hashes.SHA256()) not in _get_trusted_root_fps():
        raise ValueError(
            f"ルート CA が信頼ストア（certifi）に含まれていません: {root.subject.rfc4514_string()}"
        )


def _local_frequency_analysis(image: Image.Image) -> dict:
    """FFT周波数解析 + ノイズ解析によるヒューリスティック（参考値）"""
    img_gray = np.array(image.convert("L")).astype(np.float32)

    blurred  = np.array(image.convert("L").filter(ImageFilter.GaussianBlur(2))).astype(np.float32)
    noise_std = float(np.std(img_gray - blurred))

    fft       = np.fft.fftshift(np.fft.fft2(img_gray))
    magnitude = np.abs(fft)
    h, w      = magnitude.shape
    cy, cx    = h // 2, w // 2
    low_sum   = magnitude[cy - h//8: cy + h//8, cx - w//8: cx + w//8].sum()
    high_freq_ratio = float(1.0 - low_sum / (magnitude.sum() + 1e-9))

    if noise_std < 1.5:
        verdict = "suspicious"
        detail  = f"ノイズが少なすぎます (noise_std={noise_std:.2f})。参考値のみ。"
    else:
        verdict = "likely_real"
        detail  = f"ノイズパターンは自然です (noise_std={noise_std:.2f})。参考値のみ。"

    return {
        "verdict": verdict,
        "detail": detail,
        "noise_std": round(noise_std, 3),
        "high_freq_ratio": round(high_freq_ratio, 4),
    }


async def detect_ai_generated(raw_bytes: bytes, image: Image.Image) -> dict:
    """
    AI生成画像検出。
    HUGGINGFACE_API_KEY が設定されていれば HuggingFace Inference API を使用し、
    未設定またはエラー時はローカル周波数解析にフォールバックする。
    """
    base: dict = {"verdict": "unknown", "ai_score": None, "real_score": None,
                  "method": None, "detail": None}

    if HF_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.post(
                    _HF_MODEL_URL,
                    content=raw_bytes,
                    headers={"Authorization": f"Bearer {HF_API_KEY}",
                             "Content-Type": "application/octet-stream"},
                )
            if res.status_code == 200:
                preds      = res.json()
                scores     = {p["label"].lower(): p["score"] for p in preds}
                ai_score   = scores.get("artificial", scores.get("ai-generated", 0.0))
                real_score = scores.get("human", scores.get("real", 1.0 - ai_score))

                base["ai_score"]   = round(ai_score, 4)
                base["real_score"] = round(real_score, 4)
                base["method"]     = "huggingface:umm-maybe/AI-image-detector"

                if ai_score >= 0.85:
                    base["verdict"] = "ai_generated"
                    base["detail"]  = f"高い確率でAI生成画像 (確信度: {ai_score:.1%})"
                elif ai_score >= 0.50:
                    base["verdict"] = "suspicious"
                    base["detail"]  = f"AI生成画像の可能性あり (スコア: {ai_score:.1%})"
                else:
                    base["verdict"] = "likely_real"
                    base["detail"]  = f"本物の写真と判定 (実写スコア: {real_score:.1%})"
                return base
            else:
                base["detail"] = f"HuggingFace API エラー: HTTP {res.status_code}"
        except Exception as e:
            base["detail"] = f"HuggingFace API 接続エラー: {e}"

    # ── ローカルフォールバック ──
    base["method"] = "local:frequency_analysis"
    base.update(_local_frequency_analysis(image))
    return base


def compute_authenticity_score(exif: dict, ela: dict, file_size_bytes: int,
                                ai: dict | None = None, ts: dict | None = None) -> dict:
    """
    総合真正性スコア（0〜100）を算出する
    各要素を重み付けして集計
    """
    score = 100.0
    details = []
    deductions = []

    # ── EXIF評価（最大30点減点）──
    if not exif["has_exif"]:
        score -= 25
        deductions.append({"factor": "EXIFなし", "penalty": -25, "reason": "撮影メタデータが存在しない"})
    else:
        sw_category = exif.get("software_category")
        if sw_category == "edit":
            score -= 20
            deductions.append({"factor": "加工ソフト検出", "penalty": -20, "reason": f"加工ソフト '{exif['software']}' が検出された（合成・修正の可能性）"})
        elif sw_category == "develop":
            details.append(f"現像ソフト使用（減点なし）: {exif['software']} — RAW現像・色調整はスコアに影響しません")

        if not exif.get("camera_make") and not exif.get("camera_model"):
            score -= 5
            deductions.append({"factor": "カメラ情報なし", "penalty": -5, "reason": "カメラメーカー・モデル情報がない"})

        if not exif.get("datetime_original"):
            score -= 5
            deductions.append({"factor": "撮影日時なし", "penalty": -5, "reason": "撮影日時メタデータがない"})

    # ── ELA評価（最大50点減点）──
    verdict = ela.get("ela_verdict", "unknown")
    if verdict == "clean":
        details.append("ELA解析：加工痕跡なし")
    elif verdict == "suspicious":
        score -= 25
        deductions.append({
            "factor": "ELA要注意",
            "penalty": -25,
            "reason": f"一部に加工の可能性（疑わしいピクセル比率: {ela['ela_suspicious_ratio']:.1%}）"
        })
    elif verdict == "likely_edited":
        score -= 45
        deductions.append({
            "factor": "ELA加工検出",
            "penalty": -45,
            "reason": f"高い確率で加工あり（平均誤差: {ela['ela_mean_diff']:.2f}, 疑わしいピクセル: {ela['ela_suspicious_ratio']:.1%}）"
        })

    # ── AI生成画像検出（最大50点減点、HuggingFace API使用時のみ）──
    if ai and ai.get("method", "").startswith("huggingface:"):
        ai_verdict = ai.get("verdict", "unknown")
        if ai_verdict == "ai_generated":
            score -= 50
            deductions.append({
                "factor": "AI生成画像検出",
                "penalty": -50,
                "reason": ai.get("detail", "AI生成画像と判定された"),
            })
        elif ai_verdict == "suspicious":
            score -= 30
            deductions.append({
                "factor": "AI生成画像の疑い",
                "penalty": -30,
                "reason": ai.get("detail", "AI生成画像の可能性がある"),
            })
        elif ai_verdict == "likely_real":
            details.append(f"AI生成画像検出：本物の写真と判定 — {ai.get('detail', '')}")

    # ── ファイルサイズ評価（最大5点減点）──
    if file_size_bytes < 10_000:
        score -= 5
        deductions.append({"factor": "ファイルサイズ", "penalty": -5, "reason": "ファイルサイズが小さすぎる（10KB未満）"})

    # ── RFC 3161 タイムスタンプ（+10点ボーナス）──
    if ts:
        score += 10
        details.append(
            f"RFC 3161 タイムスタンプ取得済み (+10点) — "
            f"TSA: {ts.get('tsa', '')} · 認定: {ts.get('tsa_time', '')}"
        )

    score = max(0.0, min(100.0, score))

    # 総合判定
    if score >= 80:
        verdict_label = "高（改ざんの可能性は低い）"
        verdict_code = "high"
    elif score >= 50:
        verdict_label = "中（要確認）"
        verdict_code = "medium"
    else:
        verdict_label = "低（改ざんの疑いあり）"
        verdict_code = "low"

    return {
        "score": round(score, 1),
        "verdict": verdict_code,
        "verdict_label": verdict_label,
        "deductions": deductions,
        "details": details,
    }


# ──────────────────────────────────────────────
# エンドポイント
# ──────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def ui_index(request: Request, imprint_session: str = Cookie(default=None)):
    user_id = _decode_session_token(imprint_session or "")
    if not user_id:
        return RedirectResponse("/login", status_code=302)
    conn = _db()
    try:
        user = _get_user(conn, user_id)
        if not user:
            resp = RedirectResponse("/login", status_code=302)
            resp.delete_cookie("imprint_session")
            return resp
        # ユーザーの API キーを取得
        key_row = conn.execute(
            "SELECT name FROM api_keys WHERE user_id = ? AND is_active = 1 LIMIT 1",
            (user_id,),
        ).fetchone()
        # API キーの raw 値はDBに持たないため、ダッシュボード用の表示キーは別途発行済みのものを使う
        # フロントに渡す API キーは Cookie セッション経由で /auth/me から取得させる
        usage = _get_usage(conn, user_id)
        limit = PLAN_LIMITS.get(user["plan"])
    finally:
        conn.close()
    return _templates.TemplateResponse(
        "index.html", {
            "request":      request,
            "api_key":      "",          # JS が /auth/apikey から取得
            "network_name": _network_name(),
            "chain_id":     POLYGON_CHAIN_ID,
            "tsa_providers": [
                {"key": k, "name": v["name"], "description": v["description"]}
                for k, v in _TSA_PROVIDERS.items()
            ],
            "default_tsa":  _DEFAULT_TSA_KEY,
            "user_email":   user["email"],
            "user_plan":    user["plan"],
            "plan_label":   PLAN_LABELS.get(user["plan"], user["plan"]),
            "usage_count":  usage,
            "usage_limit":  limit if limit is not None else "無制限",
            "stripe_enabled": bool(STRIPE_SECRET_KEY and STRIPE_BUSINESS_PRICE),
        }
    )

# ──────────────────────────────────────────────
# 認証エンドポイント
# ──────────────────────────────────────────────

@app.get("/login", include_in_schema=False)
async def login_page(request: Request, imprint_session: str = Cookie(default=None)):
    if _decode_session_token(imprint_session or ""):
        return RedirectResponse("/", status_code=302)
    return _templates.TemplateResponse("login.html", {"request": request, "error": ""})


@app.get("/register", include_in_schema=False)
async def register_page(request: Request, imprint_session: str = Cookie(default=None)):
    if _decode_session_token(imprint_session or ""):
        return RedirectResponse("/", status_code=302)
    return _templates.TemplateResponse("register.html", {"request": request, "error": ""})


@app.post("/auth/register", include_in_schema=False)
async def auth_register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    if password != password_confirm:
        return _templates.TemplateResponse("register.html", {"request": request, "error": "パスワードが一致しません"}, status_code=400)
    if len(password) < 8:
        return _templates.TemplateResponse("register.html", {"request": request, "error": "パスワードは8文字以上にしてください"}, status_code=400)
    conn = _db()
    try:
        if conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            return _templates.TemplateResponse("register.html", {"request": request, "error": "このメールアドレスは既に登録されています"}, status_code=400)
        user_id   = secrets.token_hex(16)
        pw_hash   = _pwd_ctx.hash(password)
        now       = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO users (id, email, password_hash, plan, created_at) VALUES (?, ?, ?, 'starter', ?)",
            (user_id, email, pw_hash, now),
        )
        # API キーを自動発行
        raw_key  = f"imp_{secrets.token_urlsafe(32)}"
        key_id   = secrets.token_hex(8)
        conn.execute(
            "INSERT INTO api_keys (id, key_hash, name, created_at, user_id) VALUES (?, ?, ?, ?, ?)",
            (key_id, _hash_key(raw_key), email, now, user_id),
        )
        # API キーを一時テーブルに保存してダッシュボードで表示
        conn.execute(
            "INSERT OR REPLACE INTO user_api_key_display (user_id, raw_key, shown) VALUES (?, ?, 0)",
            (user_id, raw_key),
        )
        conn.commit()
    finally:
        conn.close()
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("imprint_session", _create_session_token(user_id), httponly=True, samesite="lax", max_age=86400 * JWT_EXPIRY_DAYS)
    return resp


@app.post("/auth/login", include_in_schema=False)
async def auth_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    conn = _db()
    try:
        user = conn.execute("SELECT * FROM users WHERE email = ? AND is_active = 1", (email,)).fetchone()
    finally:
        conn.close()
    if not user or not _pwd_ctx.verify(password, user["password_hash"]):
        return _templates.TemplateResponse("login.html", {"request": request, "error": "メールアドレスまたはパスワードが正しくありません"}, status_code=401)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("imprint_session", _create_session_token(user["id"]), httponly=True, samesite="lax", max_age=86400 * JWT_EXPIRY_DAYS)
    return resp


@app.get("/auth/logout", include_in_schema=False)
async def auth_logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("imprint_session")
    return resp


@app.get("/auth/me", tags=["Auth"])
async def auth_me(user=Depends(_require_user)):
    return {"id": user["id"], "email": user["email"], "plan": user["plan"]}


@app.get("/auth/profile", tags=["Auth"], include_in_schema=False)
async def auth_profile(user=Depends(_require_user)):
    conn = _db()
    try:
        usage_rows = conn.execute(
            "SELECT year_month, count FROM monthly_usage WHERE user_id = ? ORDER BY year_month DESC LIMIT 12",
            (user["id"],),
        ).fetchall()
        total = sum(r["count"] for r in usage_rows)
        key_row = conn.execute(
            "SELECT id, created_at FROM api_keys WHERE user_id = ? AND is_active = 1 LIMIT 1",
            (user["id"],),
        ).fetchone()
    finally:
        conn.close()
    limit = PLAN_LIMITS.get(user["plan"])
    return {
        "email": user["email"],
        "plan": user["plan"],
        "plan_label": PLAN_LABELS.get(user["plan"], user["plan"]),
        "created_at": user["created_at"],
        "usage_limit": limit,
        "total_analyses": total,
        "api_key_id": key_row["id"] if key_row else None,
        "api_key_created": key_row["created_at"] if key_row else None,
    }


@app.post("/auth/profile/password", tags=["Auth"], include_in_schema=False)
async def change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    user=Depends(_require_user),
):
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="パスワードは8文字以上にしてください")
    if not _pwd_ctx.verify(current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="現在のパスワードが正しくありません")
    conn = _db()
    try:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_pwd_ctx.hash(new_password), user["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/dashboard/history", tags=["Dashboard"], include_in_schema=False)
async def dashboard_history(
    page: int = 1,
    user=Depends(_require_user),
):
    per_page = 20
    offset = (page - 1) * per_page
    conn = _db()
    try:
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM analyses WHERE user_id = ?", (user["id"],)
        ).fetchone()["cnt"]
        rows = conn.execute(
            """SELECT filename, file_size, file_hash, score, verdict, ela_verdict,
                      ai_verdict, has_timestamp, has_blockchain, tx_hash, created_at
               FROM analyses WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (user["id"], per_page, offset),
        ).fetchall()
    finally:
        conn.close()
    explorer_base = _explorer_base() if CONTRACT_ADDRESS else "https://amoy.polygonscan.com"
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [
            {
                **dict(r),
                "explorer_url": f"{explorer_base}/tx/{r['tx_hash']}" if r["tx_hash"] else None,
            }
            for r in rows
        ],
    }


@app.get("/dashboard/stats", tags=["Dashboard"], include_in_schema=False)
async def dashboard_stats(user=Depends(_require_user)):
    conn = _db()
    try:
        usage_rows = conn.execute(
            "SELECT year_month, count FROM monthly_usage WHERE user_id = ? ORDER BY year_month ASC",
            (user["id"],),
        ).fetchall()
        total_analyses = conn.execute(
            "SELECT COUNT(*) as cnt FROM analyses WHERE user_id = ?", (user["id"],)
        ).fetchone()["cnt"]
        ts_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM analyses WHERE user_id = ? AND has_timestamp = 1", (user["id"],)
        ).fetchone()["cnt"]
        bc_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM analyses WHERE user_id = ? AND has_blockchain = 1", (user["id"],)
        ).fetchone()["cnt"]
        avg_score = conn.execute(
            "SELECT AVG(score) as avg FROM analyses WHERE user_id = ? AND score IS NOT NULL", (user["id"],)
        ).fetchone()["avg"]
        verdict_dist = conn.execute(
            "SELECT verdict, COUNT(*) as cnt FROM analyses WHERE user_id = ? GROUP BY verdict",
            (user["id"],),
        ).fetchall()
    finally:
        conn.close()
    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    current_usage = next((r["count"] for r in usage_rows if r["year_month"] == ym), 0)
    limit = PLAN_LIMITS.get(user["plan"])
    return {
        "total_analyses": total_analyses,
        "ts_count": ts_count,
        "bc_count": bc_count,
        "avg_score": round(avg_score, 1) if avg_score else None,
        "current_usage": current_usage,
        "usage_limit": limit,
        "monthly_usage": [{"month": r["year_month"], "count": r["count"]} for r in usage_rows],
        "verdict_dist": [{"verdict": r["verdict"], "count": r["cnt"]} for r in verdict_dist],
    }


@app.get("/auth/apikey", tags=["Auth"], include_in_schema=False)
async def auth_apikey(user=Depends(_require_user)):
    """ダッシュボード用: ユーザーの API キーを返す（初回のみ raw キーを表示）"""
    conn = _db()
    try:
        disp = conn.execute("SELECT raw_key, shown FROM user_api_key_display WHERE user_id = ?", (user["id"],)).fetchone()
        if disp and not disp["shown"]:
            conn.execute("UPDATE user_api_key_display SET shown = 1 WHERE user_id = ?", (user["id"],))
            conn.commit()
            return {"raw_key": disp["raw_key"], "first_time": True}
        row = conn.execute("SELECT id FROM api_keys WHERE user_id = ? AND is_active = 1 LIMIT 1", (user["id"],)).fetchone()
        return {"key_id": row["id"] if row else None, "first_time": False}
    finally:
        conn.close()


# ──────────────────────────────────────────────
# 課金エンドポイント（Stripe）
# ──────────────────────────────────────────────

@app.post("/billing/checkout", tags=["Billing"])
async def billing_checkout(user=Depends(_require_user)):
    if not STRIPE_SECRET_KEY or not STRIPE_BUSINESS_PRICE:
        raise HTTPException(status_code=503, detail="課金機能が設定されていません")
    session = stripe_lib.checkout.Session.create(
        customer_email=user["email"],
        line_items=[{"price": STRIPE_BUSINESS_PRICE, "quantity": 1}],
        mode="subscription",
        success_url=f"{SITE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{SITE_URL}/",
        metadata={"user_id": user["id"]},
    )
    return {"url": session.url}


@app.get("/billing/success", include_in_schema=False)
async def billing_success(request: Request, imprint_session: str = Cookie(default=None)):
    user_id = _decode_session_token(imprint_session or "")
    if not user_id:
        return RedirectResponse("/login", status_code=302)
    return _templates.TemplateResponse("billing_success.html", {"request": request})


@app.post("/billing/webhook", tags=["Billing"])
async def billing_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook が設定されていません")
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe_lib.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Webhook 署名が無効です")
    conn = _db()
    try:
        if event["type"] == "checkout.session.completed":
            s = event["data"]["object"]
            user_id = (s.get("metadata") or {}).get("user_id")
            if user_id:
                conn.execute("UPDATE users SET plan = 'business', stripe_customer_id = ? WHERE id = ?",
                             (s.get("customer"), user_id))
                conn.commit()
        elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
            customer_id = event["data"]["object"].get("customer")
            if customer_id:
                conn.execute("UPDATE users SET plan = 'starter' WHERE stripe_customer_id = ?", (customer_id,))
                conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


# ──────────────────────────────────────────────
# 管理 API: ユーザー管理
# ──────────────────────────────────────────────

@app.get("/admin/users", summary="ユーザー一覧", tags=["Admin"])
def list_users(_: None = Depends(require_admin)):
    conn = _db()
    try:
        rows = conn.execute("SELECT id, email, plan, created_at, is_active FROM users ORDER BY created_at DESC").fetchall()
        return {"users": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.put("/admin/users/{user_id}/plan", summary="プラン変更", tags=["Admin"])
def update_user_plan(user_id: str, plan: str = Body(..., embed=True), _: None = Depends(require_admin)):
    if plan not in PLAN_LIMITS:
        raise HTTPException(status_code=400, detail=f"不明なプラン: {plan}")
    conn = _db()
    try:
        result = conn.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
        conn.commit()
        return {"user_id": user_id, "plan": plan}
    finally:
        conn.close()


@app.get("/health")
def health():
    bc_ready = bool(POLYGON_RPC_URL and CONTRACT_ADDRESS)
    return {
        "service": "Imprint API",
        "version": app.version,
        "status": "running",
        "blockchain": {
            "configured": bc_ready,
            "network": _network_name(),
            "chain_id": POLYGON_CHAIN_ID,
            "contract": CONTRACT_ADDRESS if CONTRACT_ADDRESS else None,
            "explorer": _explorer_base() if bc_ready else None,
        },
    }


# ──────────────────────────────────────────────
# 管理API（APIキー管理） — X-Admin-Key 必須
# ──────────────────────────────────────────────

@app.post("/admin/keys", summary="APIキーを発行する", tags=["Admin"])
def create_api_key(
    name: str = Body(..., embed=True, description="顧客名・用途など識別用ラベル"),
    _: None = Depends(require_admin),
):
    """
    新しいAPIキーを発行します。キーはこのレスポンスにのみ含まれます（再表示不可）。
    """
    raw_key = f"imp_{secrets.token_urlsafe(32)}"
    key_id = secrets.token_hex(8)
    created_at = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO api_keys (id, key_hash, name, created_at) VALUES (?, ?, ?, ?)",
            (key_id, _hash_key(raw_key), name, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "key_id": key_id,
        "api_key": raw_key,
        "name": name,
        "created_at": created_at,
        "note": "このキーは一度しか表示されません。安全に保管してください。",
    }


@app.get("/admin/keys", summary="APIキー一覧を取得する", tags=["Admin"])
def list_api_keys(_: None = Depends(require_admin)):
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, name, created_at, is_active, req_count, last_used "
            "FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        return {"keys": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.delete("/admin/keys/{key_id}", summary="APIキーを無効化する", tags=["Admin"])
def revoke_api_key(key_id: str, _: None = Depends(require_admin)):
    conn = _db()
    try:
        result = conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE id = ?", (key_id,)
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="指定したキーIDが見つかりません")
        return {"key_id": key_id, "status": "revoked"}
    finally:
        conn.close()


# ──────────────────────────────────────────────
# ブロックチェーン記録（Polygon）
# ──────────────────────────────────────────────

POLYGON_RPC_URL      = os.getenv("POLYGON_RPC_URL", "")
POLYGON_PRIVATE_KEY  = os.getenv("POLYGON_PRIVATE_KEY", "")
CONTRACT_ADDRESS     = os.getenv("IMPRINT_CONTRACT_ADDRESS", "")
POLYGON_CHAIN_ID     = int(os.getenv("POLYGON_CHAIN_ID", "80002"))  # 80002=Amoy testnet, 137=mainnet

# ImprintRegistry コントラクトの ABI（contracts/ImprintRegistry.sol と同期）
_REGISTRY_ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "imageHash", "type": "bytes32"},
            {"internalType": "string",  "name": "metadata",  "type": "string"},
        ],
        "name": "register",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "bytes32", "name": "imageHash", "type": "bytes32"}],
        "name": "verify",
        "outputs": [
            {"internalType": "bool",    "name": "exists",    "type": "bool"},
            {"internalType": "address", "name": "registrar", "type": "address"},
            {"internalType": "uint256", "name": "timestamp", "type": "uint256"},
            {"internalType": "string",  "name": "metadata",  "type": "string"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "internalType": "bytes32", "name": "imageHash", "type": "bytes32"},
            {"indexed": True,  "internalType": "address", "name": "registrar", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "timestamp", "type": "uint256"},
        ],
        "name": "HashRegistered",
        "type": "event",
    },
]


def _network_name() -> str:
    return "Polygon Mainnet" if POLYGON_CHAIN_ID == 137 else "Polygon Amoy Testnet"


def _explorer_url(tx_hex: str) -> str:
    base = "https://polygonscan.com" if POLYGON_CHAIN_ID == 137 else "https://amoy.polygonscan.com"
    return f"{base}/tx/{tx_hex}"


def _explorer_base() -> str:
    return "https://polygonscan.com" if POLYGON_CHAIN_ID == 137 else "https://amoy.polygonscan.com"


def _build_gas_params(w3) -> dict:
    """EIP-1559 対応ネットワークなら maxFeePerGas/maxPriorityFeePerGas、非対応なら legacy gasPrice を返す。"""
    try:
        latest = w3.eth.get_block("latest")
        if latest.get("baseFeePerGas") is not None:
            base_fee = latest["baseFeePerGas"]
            try:
                priority_fee = w3.eth.max_priority_fee
            except Exception:
                priority_fee = w3.to_wei(30, "gwei")
            return {
                "maxFeePerGas": base_fee * 2 + priority_fee,
                "maxPriorityFeePerGas": priority_fee,
            }
    except Exception:
        pass
    return {"gasPrice": w3.eth.gas_price}


def _get_w3():
    try:
        from web3 import Web3
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="web3 パッケージが必要です: pip install 'web3>=6'",
        )
    if not POLYGON_RPC_URL:
        raise HTTPException(status_code=503, detail="POLYGON_RPC_URL が設定されていません")
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
    if not w3.is_connected():
        raise HTTPException(status_code=503, detail="ブロックチェーンノードへの接続に失敗しました")
    return w3


def _get_contract(w3):
    from web3 import Web3
    if not CONTRACT_ADDRESS:
        raise HTTPException(status_code=503, detail="IMPRINT_CONTRACT_ADDRESS が設定されていません")
    return w3.eth.contract(
        address=Web3.to_checksum_address(CONTRACT_ADDRESS),
        abi=_REGISTRY_ABI,
    )


@app.post("/blockchain/register", summary="写真ハッシュをブロックチェーンに記録する", tags=["Blockchain"])
async def blockchain_register(
    file: UploadFile = File(...),
    _key_id: str = Depends(require_api_key_limited),
    x_api_key: str = Depends(_api_key_header),
):
    """
    画像のSHA-256ハッシュと真正性スコアをPolygonチェーンに永久記録します。

    - 同一ハッシュが既登録の場合は登録情報を返します（冪等）
    - 必要な環境変数: POLYGON_RPC_URL, POLYGON_PRIVATE_KEY, IMPRINT_CONTRACT_ADDRESS
    """
    from web3 import Web3

    raw_bytes = await file.read()
    if len(raw_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="ファイルサイズは20MB以下にしてください")

    sha256_hex = compute_sha256(raw_bytes)
    hash_bytes32 = bytes.fromhex(sha256_hex)

    # 真正性スコアを計算してメタデータに含める
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        exif_data = extract_exif(image)
        ela_data = ela_analysis(image)
        auth = compute_authenticity_score(exif_data, ela_data, len(raw_bytes))
        score = auth["score"]
        verdict = auth["verdict"]
    except Exception:
        score, verdict = None, None

    on_chain_meta = json.dumps({
        "filename": file.filename,
        "size_bytes": len(raw_bytes),
        "score": score,
        "verdict": verdict,
        "api_version": app.version,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False)

    w3 = _get_w3()
    contract = _get_contract(w3)

    # 既登録チェック（view関数 = ガス不要）
    exists, registrar, on_chain_ts, existing_meta = contract.functions.verify(hash_bytes32).call()
    if exists:
        return {
            "status": "already_registered",
            "hash": sha256_hex,
            "network": _network_name(),
            "registrar": registrar,
            "registered_at": datetime.fromtimestamp(on_chain_ts, tz=timezone.utc).isoformat(),
            "metadata": json.loads(existing_meta) if existing_meta else None,
        }

    # トランザクション送信
    if not POLYGON_PRIVATE_KEY:
        raise HTTPException(status_code=503, detail="POLYGON_PRIVATE_KEY が設定されていません")

    pk = POLYGON_PRIVATE_KEY if POLYGON_PRIVATE_KEY.startswith("0x") else f"0x{POLYGON_PRIVATE_KEY}"
    account = w3.eth.account.from_key(pk)
    nonce = w3.eth.get_transaction_count(account.address)

    # ガスを動的に見積もり（20%バッファ付き）
    gas_estimate = contract.functions.register(hash_bytes32, on_chain_meta).estimate_gas(
        {"from": account.address}
    )
    gas_limit = int(gas_estimate * 1.2)

    tx = contract.functions.register(hash_bytes32, on_chain_meta).build_transaction({
        "chainId": POLYGON_CHAIN_ID,
        "from": account.address,
        "nonce": nonce,
        "gas": gas_limit,
        **_build_gas_params(w3),
    })
    signed = account.sign_transaction(tx)
    tx_hash_bytes = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash_bytes, timeout=120)

    tx_hex = tx_hash_bytes.hex()
    if receipt["status"] == 0:
        raise HTTPException(
            status_code=500,
            detail=f"トランザクションがチェーン上で失敗しました。TX: {tx_hex} / "
                   f"Polygonscan: {_explorer_url(tx_hex)}",
        )

    # analyses のブロックチェーンフラグを更新
    if x_api_key:
        _bc_conn = _db()
        try:
            _bc_row = _bc_conn.execute(
                "SELECT user_id FROM api_keys WHERE key_hash = ? AND is_active = 1",
                (_hash_key(x_api_key),),
            ).fetchone()
            if _bc_row and _bc_row["user_id"]:
                _bc_conn.execute(
                    "UPDATE analyses SET has_blockchain = 1, tx_hash = ? WHERE user_id = ? AND file_hash = ?",
                    (tx_hex, _bc_row["user_id"], sha256_hex),
                )
                _bc_conn.commit()
        finally:
            _bc_conn.close()

    return {
        "status": "registered",
        "hash": sha256_hex,
        "network": _network_name(),
        "tx_hash": tx_hex,
        "block_number": receipt["blockNumber"],
        "gas_used": receipt["gasUsed"],
        "registrar": account.address,
        "explorer_url": _explorer_url(tx_hex),
        "metadata": json.loads(on_chain_meta),
    }


@app.get(
    "/blockchain/status/{sha256_hex}",
    summary="ブロックチェーン上の登録状態を確認する",
    tags=["Blockchain"],
)
async def blockchain_status(
    sha256_hex: str,
    _key_id: str = Depends(require_api_key),
):
    """
    指定したSHA-256ハッシュがPolygonチェーンに記録されているか照会します（読み取り専用・無料）。

    - 必要な環境変数: POLYGON_RPC_URL, IMPRINT_CONTRACT_ADDRESS
    """
    if len(sha256_hex) != 64 or not all(c in "0123456789abcdefABCDEF" for c in sha256_hex):
        raise HTTPException(status_code=400, detail="有効なSHA-256ハッシュ（64文字16進数）を指定してください")

    hash_bytes32 = bytes.fromhex(sha256_hex)
    w3 = _get_w3()
    contract = _get_contract(w3)

    exists, registrar, on_chain_ts, existing_meta = contract.functions.verify(hash_bytes32).call()

    if not exists:
        return {"registered": False, "hash": sha256_hex, "network": _network_name()}

    return {
        "registered": True,
        "hash": sha256_hex,
        "network": _network_name(),
        "registrar": registrar,
        "registered_at": datetime.fromtimestamp(on_chain_ts, tz=timezone.utc).isoformat(),
        "metadata": json.loads(existing_meta) if existing_meta else None,
    }


# ──────────────────────────────────────────────
# RFC 3161 タイムスタンプ認証
# ──────────────────────────────────────────────

async def _fetch_ts_token(raw_bytes: bytes, tsa_url: str) -> tuple[bytes, str, str | None]:
    """
    RFC 3161 タイムスタンプトークンを指定 TSA から取得する。
    Returns (response_bytes, tsa_time_iso, serial_no)
    """
    import rfc3161ng
    from rfc3161ng import encode_timestamp_request
    from pyasn1.codec.der import decoder as asn1_decoder
    from pyasn1.type import univ as asn1_univ

    # SHA-256 ダイジェストを事前計算して渡す（rfc3161ng の data= はハッシュ名を無視するバグあり）
    digest = hashlib.sha256(raw_bytes).digest()
    nonce = secrets.randbits(64)
    ts_req_obj = rfc3161ng.make_timestamp_request(
        digest=digest,
        hashname="sha256",
        include_tsa_certificate=True,
        nonce=nonce,
    )
    ts_req_bytes = encode_timestamp_request(ts_req_obj)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            tsa_url,
            content=ts_req_bytes,
            headers={"Content-Type": "application/timestamp-query"},
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"TSA エラー: HTTP {resp.status_code}")

    try:
        ts_resp = rfc3161ng.decode_timestamp_response(resp.content)
        tst = ts_resp.time_stamp_token
        tsa_time = rfc3161ng.get_timestamp(tst, naive=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TSA レスポンスの解析に失敗: {e}")

    # TSTInfo からシリアル番号を取得
    serial_no = None
    try:
        tstinfo_raw = tst.getComponentByName("content").getComponentByPosition(2).getComponentByPosition(1)
        tstinfo_bytes, _ = asn1_decoder.decode(tstinfo_raw, asn1Spec=asn1_univ.OctetString())
        tstinfo, _ = asn1_decoder.decode(bytes(tstinfo_bytes), asn1Spec=rfc3161ng.TSTInfo())
        serial_no = str(tstinfo.getComponentByName("serialNumber"))
    except Exception:
        pass

    return resp.content, tsa_time.isoformat(), serial_no


def _check_timestamp_rsa_or_ec(tst, digest: bytes) -> None:
    """
    RFC 3161 TST の署名をメッセージインプリント + RSA/ECDSA で検証する。
    rfc3161ng.check_timestamp は RSA (PKCS1v15) 専用のため、
    FreeTSA.org などが ECDSA 証明書を使うと TypeError になる。本関数はその両方に対応する。
    """
    from rfc3161ng.api import (
        load_certificate, get_hash_from_oid, get_hash_class_from_oid,
        id_attribute_messageDigest, decoder as asn1_dec, encoder as asn1_enc,
    )
    from pyasn1.type import univ as asn1_univ
    from cryptography.hazmat.primitives.asymmetric import ec as ec_alg, padding as rsa_padding
    from cryptography.hazmat.primitives import hashes as crypto_hashes
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey as _RSAKey
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey as _ECKey

    signed_data = tst.content
    cert = load_certificate(signed_data, b"")

    # メッセージインプリントの検証
    tstinfo_raw = (
        tst.getComponentByName("content")
           .getComponentByPosition(2)
           .getComponentByPosition(1)
    )
    tstinfo_bytes, _ = asn1_dec.decode(tstinfo_raw, asn1Spec=asn1_univ.OctetString())
    import rfc3161ng as _rfc3161ng
    tstinfo, _ = asn1_dec.decode(bytes(tstinfo_bytes), asn1Spec=_rfc3161ng.TSTInfo())
    hashed_msg = bytes(
        tstinfo.getComponentByName("messageImprint").getComponentByName("hashedMessage")
    )
    if hashed_msg != digest:
        raise ValueError("Message imprint mismatch")

    # 署名対象データの構築
    signer_info = signed_data["signerInfos"][0]
    signer_digest_alg = signer_info["digestAlgorithm"]["algorithm"]
    signer_hash_name = get_hash_from_oid(signer_digest_alg)
    signer_hash_cls = get_hash_class_from_oid(signer_digest_alg)

    content = bytes(asn1_dec.decode(
        bytes(tst.content["contentInfo"]["content"]),
        asn1Spec=asn1_univ.OctetString(),
    )[0])
    content_digest = signer_hash_cls(content).digest()

    auth_attrs = signer_info["authenticatedAttributes"]
    if len(auth_attrs):
        for attr in auth_attrs:
            if attr[0] == id_attribute_messageDigest:
                signed_digest = bytes(asn1_dec.decode(bytes(attr[1][0]), asn1Spec=asn1_univ.OctetString())[0])
                if signed_digest != content_digest:
                    raise ValueError("Content digest mismatch")
                s = asn1_univ.SetOf()
                for i, x in enumerate(auth_attrs):
                    s.setComponentByPosition(i, x)
                signed_bytes = asn1_enc.encode(s)
                break
        else:
            raise ValueError("No message digest attribute found")
    else:
        signed_bytes = content

    # 署名の検証（RSA / ECDSA 両対応）
    signature = bytes(signer_info["encryptedDigest"])
    pub_key = cert.public_key()
    hash_alg = getattr(crypto_hashes, signer_hash_name.upper())()

    if isinstance(pub_key, _ECKey):
        pub_key.verify(signature, signed_bytes, ec_alg.ECDSA(hash_alg))
    else:
        pub_key.verify(signature, signed_bytes, rsa_padding.PKCS1v15(), hash_alg)


def _verify_cert_chain(tst, ca_pem: bytes) -> None:
    """
    TST に埋め込まれた証明書チェーンをルート CA まで検証する。
    中間 CA を含む複数段チェーンに対応する。

    手順:
    1. SignedData の certificates フィールドから全証明書を抽出
    2. SignerInfo の issuerAndSerialNumber で署名証明書を特定
    3. issuer 名を辿って signer → (intermediates) → root CA のチェーンを組み立て
    4. verify_directly_issued_by() で各リンクを暗号学的に検証
    """
    from cryptography import x509
    from pyasn1.codec.der import encoder as asn1_enc

    signed_data = tst.content

    # ── 埋め込み証明書を全件抽出 ──────────────────────────────────────────────
    embedded: list[x509.Certificate] = []
    certs_field = signed_data.getComponentByName("certificates")
    if certs_field is not None and certs_field.hasValue():
        for cert_choice in certs_field:
            # CertificateSet は CHOICE 型のため getComponent() で実体を取り出す
            for get_der in (
                lambda c: asn1_enc.encode(c.getComponent()),
                lambda c: asn1_enc.encode(c),
            ):
                try:
                    embedded.append(x509.load_der_x509_certificate(get_der(cert_choice)))
                    break
                except Exception:
                    pass

    if not embedded:
        raise ValueError("証明書チェーンが TST に含まれていません")

    # ── ルート CA をロード ───────────────────────────────────────────────────
    ca_cert: x509.Certificate = (
        x509.load_pem_x509_certificate(ca_pem)
        if b"-----BEGIN" in ca_pem
        else x509.load_der_x509_certificate(ca_pem)
    )

    # ── 署名証明書を特定（SignerInfo の issuerAndSerialNumber で照合） ──────────
    signer_info = signed_data["signerInfos"][0]
    try:
        signer_serial = int(
            signer_info["signerIdentifier"]["issuerAndSerialNumber"]["serialNumber"]
        )
        signer_cert = next(
            (c for c in embedded if c.serial_number == signer_serial), embedded[0]
        )
    except Exception:
        signer_cert = embedded[0]

    def _names_equal(a: x509.Name, b: x509.Name) -> bool:
        # DER 比較のほか RFC 4514 文字列比較もフォールバックとして使う
        # （PrintableString vs UTF8String の違いを吸収するため）
        return a == b or a.rfc4514_string() == b.rfc4514_string()

    # ── issuer 名を辿ってチェーンを組み立てる ──────────────────────────────────
    chain: list[x509.Certificate] = [signer_cert]
    seen_serials: set[int] = {signer_cert.serial_number}
    current = signer_cert

    for _ in range(8):  # 最大 8 段の中間 CA
        if _names_equal(current.issuer, ca_cert.subject):
            chain.append(ca_cert)
            break
        issuer = next(
            (
                c for c in embedded
                if c.serial_number not in seen_serials
                and _names_equal(c.subject, current.issuer)
            ),
            None,
        )
        if issuer is None:
            raise ValueError(
                f"中間証明書が見つかりません: issuer={current.issuer.rfc4514_string()}"
            )
        chain.append(issuer)
        seen_serials.add(issuer.serial_number)
        current = issuer
    else:
        raise ValueError("証明書チェーンが深すぎます（最大 8 段）")

    if chain[-1].serial_number != ca_cert.serial_number:
        raise ValueError("チェーンがルート CA に到達しませんでした")

    # ── 各リンクを暗号学的に検証 ─────────────────────────────────────────────
    for i in range(len(chain) - 1):
        chain[i].verify_directly_issued_by(chain[i + 1])


def _query_timestamp(image_hash: str) -> dict | None:
    """DB からタイムスタンプ情報を取得する（同期）"""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT tsa_url, tsa_time, serial_no, requested_at FROM timestamps WHERE image_hash = ?",
            (image_hash,),
        ).fetchone()
        if row:
            return {
                "tsa": row["tsa_url"],
                "tsa_time": row["tsa_time"],
                "serial_no": row["serial_no"],
                "requested_at": row["requested_at"],
            }
        return None
    finally:
        conn.close()


@app.get("/timestamp/providers", summary="利用可能な TSA プロバイダー一覧", tags=["Timestamp"])
async def list_timestamp_providers(_key_id: str = Depends(require_api_key)):
    """対応するタイムスタンプ局（TSA）の一覧と、CA チェーン検証の対応状況を返します。"""
    return {
        "providers": [
            {
                "key": k,
                "name": v["name"],
                "url": v["url"],
                "description": v["description"],
                "chain_verification": True,
                "verification_method": "cacert_url" if v["ca_cert_url"] else "aia",
            }
            for k, v in _TSA_PROVIDERS.items()
        ],
        "default": _DEFAULT_TSA_KEY,
    }


@app.post("/timestamp/request", summary="RFC 3161 タイムスタンプを付与する", tags=["Timestamp"])
async def timestamp_request(
    file: UploadFile = File(...),
    tsa: str = Form(default=None),
    _key_id: str = Depends(require_api_key_limited),
    x_api_key: str = Depends(_api_key_header),
):
    """
    画像の SHA-256 ハッシュに対して RFC 3161 準拠のタイムスタンプを TSA に要求し、
    トークンを DB に保存します。

    - `tsa`: TSA プロバイダーキー（`freetsa` / `digicert` / `sectigo`）。省略時はデフォルト TSA
    - 同一ハッシュに既にタイムスタンプが付与済みの場合はその情報を返します（冪等）
    """
    try:
        import rfc3161ng  # noqa: F401
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="rfc3161ng パッケージが必要です: pip install rfc3161ng",
        )

    # TSA キーを解決（不正なキーはデフォルトにフォールバック）
    tsa_key = tsa if tsa in _TSA_PROVIDERS else _DEFAULT_TSA_KEY
    tsa_url = _TSA_PROVIDERS[tsa_key]["url"]

    raw_bytes = await file.read()
    if len(raw_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="ファイルサイズは20MB以下にしてください")

    image_hash = compute_sha256(raw_bytes)

    # 冪等チェック
    conn = _db()
    try:
        row = conn.execute(
            "SELECT tsa_url, tsa_time, serial_no, requested_at FROM timestamps WHERE image_hash = ?",
            (image_hash,),
        ).fetchone()
        if row:
            return {
                "status": "already_issued",
                "hash": image_hash,
                "tsa": row["tsa_url"],
                "tsa_time": row["tsa_time"],
                "serial_no": row["serial_no"],
                "requested_at": row["requested_at"],
                "verify_url": f"/timestamp/verify/{image_hash}",
            }
    finally:
        conn.close()

    try:
        token_bytes, tsa_time_iso, serial_no = await _fetch_ts_token(raw_bytes, tsa_url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TSA 要求に失敗: {e}")

    token_b64 = base64.b64encode(token_bytes).decode()
    ts_id = secrets.token_hex(8)
    requested_at = datetime.now(timezone.utc).isoformat()

    conn = _db()
    try:
        conn.execute(
            "INSERT INTO timestamps "
            "(id, image_hash, tsa_url, token_b64, serial_no, tsa_time, requested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts_id, image_hash, tsa_url, token_b64, serial_no, tsa_time_iso, requested_at),
        )
        # analyses のタイムスタンプフラグを更新
        if x_api_key:
            uid_row = conn.execute(
                "SELECT user_id FROM api_keys WHERE key_hash = ? AND is_active = 1",
                (_hash_key(x_api_key),),
            ).fetchone()
            if uid_row and uid_row["user_id"]:
                conn.execute(
                    "UPDATE analyses SET has_timestamp = 1 WHERE user_id = ? AND file_hash = ?",
                    (uid_row["user_id"], image_hash),
                )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "issued",
        "hash": image_hash,
        "tsa": tsa_url,
        "tsa_time": tsa_time_iso,
        "serial_no": serial_no,
        "requested_at": requested_at,
        "token_b64": token_b64,
        "verify_url": f"/timestamp/verify/{image_hash}",
    }


@app.get(
    "/timestamp/verify/{sha256_hex}",
    summary="タイムスタンプトークンを検証する",
    tags=["Timestamp"],
)
async def timestamp_verify(
    sha256_hex: str,
    _key_id: str = Depends(require_api_key),
):
    """
    DB に保存済みの RFC 3161 タイムスタンプトークンを検証します。

    - 構文チェック（フォーマット検証）
    - 埋め込み TSA 証明書による署名検証（`include_tsa_certificate=True` で取得したトークンのみ）
    - CA 証明書チェーン検証（中間 CA を含む複数段チェーンに対応）
    """
    if len(sha256_hex) != 64 or not all(c in "0123456789abcdefABCDEF" for c in sha256_hex):
        raise HTTPException(
            status_code=400,
            detail="有効な SHA-256 ハッシュ（64文字16進数）を指定してください",
        )

    conn = _db()
    try:
        row = conn.execute(
            "SELECT image_hash, tsa_url, token_b64, serial_no, tsa_time, requested_at "
            "FROM timestamps WHERE image_hash = ?",
            (sha256_hex,),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail="このハッシュのタイムスタンプは登録されていません",
            )
        row = dict(row)
    finally:
        conn.close()

    import rfc3161ng
    token_bytes = base64.b64decode(row["token_b64"])

    valid_format = False
    signature_verified = False
    parse_error = None
    sig_error = None

    try:
        ts_resp = rfc3161ng.decode_timestamp_response(token_bytes)
        tst = ts_resp.time_stamp_token
        rfc3161ng.get_timestamp(tst, naive=False)
        valid_format = True
    except Exception as e:
        parse_error = str(e)

    if valid_format:
        try:
            digest = bytes.fromhex(sha256_hex)
            _check_timestamp_rsa_or_ec(tst, digest)
            signature_verified = True
        except Exception as e:
            sig_error = str(e)

    chain_verified: bool | None = None
    chain_error: str | None = None
    if signature_verified:
        try:
            ca_pem = await _fetch_tsa_cacert(row["tsa_url"])
            if ca_pem is not None:
                # FreeTSA.org など ca_cert_url が明示されている TSA: 固定 CA を使用
                _verify_cert_chain(tst, ca_pem)
            else:
                # DigiCert, Sectigo など: AIA 拡張で中間証明書を自動取得し certifi で検証
                await _verify_cert_chain_aia(tst)
            chain_verified = True
        except Exception as e:
            chain_verified = False
            chain_error = str(e)

    return {
        "hash": sha256_hex,
        "valid_format": valid_format,
        "signature_verified": signature_verified,
        "chain_verified": chain_verified,
        "parse_error": parse_error,
        "signature_error": sig_error,
        "chain_error": chain_error,
        "tsa": row["tsa_url"],
        "tsa_time": row["tsa_time"],
        "serial_no": row["serial_no"],
        "requested_at": row["requested_at"],
    }


def build_certificate_pdf(verify_result: dict) -> bytes:
    """検証結果から日本語PDF証明書を生成する"""
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    # 日本語フォント登録
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiMin-W3"))
    JA      = "HeiseiKakuGo-W5"   # ゴシック体（見出し・UI）
    JA_SERIF = "HeiseiMin-W3"      # 明朝体（本文）

    # カラー定義
    NAVY   = colors.HexColor("#0f172a")
    INDIGO = colors.HexColor("#4f46e5")
    SLATE  = colors.HexColor("#64748b")
    BORDER = colors.HexColor("#e2e8f0")
    LIGHT  = colors.HexColor("#f8fafc")
    WHITE  = colors.white

    auth    = verify_result["authenticity"]
    score   = auth["score"]
    verdict = auth["verdict"]
    SCORE_C = (
        colors.HexColor("#16a34a") if verdict == "high"
        else colors.HexColor("#d97706") if verdict == "medium"
        else colors.HexColor("#dc2626")
    )
    VERDICT_JP = {
        "high":   ("真正性：高",   "本写真は多層解析の結果、高い真正性が認められます。"),
        "medium": ("真正性：中",   "本写真は一部に注意すべき点が検出されました。"),
        "low":    ("真正性：低",   "本写真は加工・改ざんの疑いが検出されました。"),
    }
    verdict_label, verdict_desc = VERDICT_JP.get(verdict, (verdict, ""))

    cert_no  = verify_result["hash"]["value"][:16].upper()
    now_str  = datetime.now(timezone.utc).strftime("%Y年%m月%d日  %H:%M UTC")

    PAGE_W, PAGE_H = A4
    BORDER_M = 10 * mm    # 外枠の余白
    HEADER_H = 38 * mm    # ヘッダーの高さ
    SCORE_H  = 32 * mm    # スコアバナーの高さ
    FOOT_H   = 20 * mm    # フッターの高さ
    ML       = 18 * mm    # コンテンツ左右マージン
    CW       = PAGE_W - 2 * ML   # コンテンツ幅

    buf = io.BytesIO()

    # ── ページ装飾を描画するコールバック ──
    def draw_page_decoration(canvas_obj, doc):
        c = canvas_obj
        c.saveState()

        # 外枠（二重線）
        c.setStrokeColor(colors.HexColor("#334155"))
        c.setLineWidth(1.5)
        c.rect(BORDER_M, BORDER_M, PAGE_W - 2*BORDER_M, PAGE_H - 2*BORDER_M, fill=0, stroke=1)
        c.setStrokeColor(colors.HexColor("#cbd5e1"))
        c.setLineWidth(0.4)
        c.rect(BORDER_M + 1.5*mm, BORDER_M + 1.5*mm,
               PAGE_W - 2*BORDER_M - 3*mm, PAGE_H - 2*BORDER_M - 3*mm, fill=0, stroke=1)

        # ── ヘッダー背景 ──
        hdr_y = PAGE_H - BORDER_M - HEADER_H
        c.setFillColor(NAVY)
        c.rect(BORDER_M, hdr_y, PAGE_W - 2*BORDER_M, HEADER_H, fill=1, stroke=0)

        # インディゴのアクセントライン（ヘッダー下端）
        c.setFillColor(INDIGO)
        c.rect(BORDER_M, hdr_y, PAGE_W - 2*BORDER_M, 2*mm, fill=1, stroke=0)

        # ロゴ "Imprint"
        c.setFillColor(WHITE)
        c.setFont(JA, 22)
        c.drawString(ML, hdr_y + 24*mm, "Imprint")

        # ロゴ下のタグライン
        c.setFont(JA, 7.5)
        c.setFillColor(colors.HexColor("#94a3b8"))
        c.drawString(ML, hdr_y + 19*mm, "Photo Authenticity Service")

        # 区切り縦線
        c.setStrokeColor(colors.HexColor("#334155"))
        c.setLineWidth(0.8)
        c.line(ML + 42*mm, hdr_y + 8*mm, ML + 42*mm, hdr_y + 32*mm)

        # 右側タイトル
        c.setFillColor(WHITE)
        c.setFont(JA, 15)
        title_text = "写真真正性認証証明書"
        tw = c.stringWidth(title_text, JA, 15)
        c.drawString(PAGE_W - ML - tw, hdr_y + 26*mm, title_text)

        # 証明書番号・発行日時
        c.setFont(JA, 7.5)
        c.setFillColor(colors.HexColor("#94a3b8"))
        c.drawRightString(PAGE_W - ML, hdr_y + 20*mm, f"証明書番号：IMP-{cert_no}")
        c.drawRightString(PAGE_W - ML, hdr_y + 14.5*mm, f"発 行 日 時：{now_str}")
        c.drawRightString(PAGE_W - ML, hdr_y + 9*mm,   "発 行 機 関：Imprint Photo Authenticity Service")

        # ── スコアバナー ──
        sb_y = hdr_y - SCORE_H
        c.setFillColor(LIGHT)
        c.rect(BORDER_M, sb_y, PAGE_W - 2*BORDER_M, SCORE_H, fill=1, stroke=0)

        # スコア数値
        c.setFont(JA, 44)
        c.setFillColor(SCORE_C)
        c.drawString(ML, sb_y + 14*mm, str(score))
        score_w = c.stringWidth(str(score), JA, 44)

        # / 100点
        c.setFont(JA, 13)
        c.setFillColor(colors.HexColor("#94a3b8"))
        c.drawString(ML + score_w + 2.5*mm, sb_y + 16*mm, "/ 100点")

        # 判定ラベル
        c.setFont(JA, 14)
        c.setFillColor(SCORE_C)
        c.drawString(ML + 52*mm, sb_y + 22*mm, f"● {verdict_label}")

        # 判定説明文
        c.setFont(JA, 8)
        c.setFillColor(SLATE)
        c.drawString(ML + 52*mm, sb_y + 16*mm, verdict_desc)
        c.drawString(ML + 52*mm, sb_y + 11*mm, "多層解析（EXIF・ELA・AI検出・ハッシュ・タイムスタンプ）による総合評価")

        # スコアプログレスバー
        bar_x, bar_y, bar_h = ML, sb_y + 4.5*mm, 3.5*mm
        c.setFillColor(BORDER)
        c.roundRect(bar_x, bar_y, CW, bar_h, 1.5*mm, fill=1, stroke=0)
        fill_w = max(CW * score / 100, 4*mm)
        c.setFillColor(SCORE_C)
        c.roundRect(bar_x, bar_y, fill_w, bar_h, 1.5*mm, fill=1, stroke=0)

        # バナー下境界線
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.5)
        c.line(BORDER_M, sb_y, PAGE_W - BORDER_M, sb_y)

        # ── フッター ──
        foot_top = BORDER_M + FOOT_H
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.5)
        c.line(ML, foot_top, PAGE_W - ML, foot_top)

        c.setFont(JA, 6.8)
        c.setFillColor(colors.HexColor("#94a3b8"))
        c.drawString(ML, foot_top - 4*mm,
                     "本証明書は Imprint Photo Authenticity Service（https://imprint-digital.jp）が発行した電子文書です。")
        c.drawString(ML, foot_top - 8*mm,
                     "記載内容は参考情報です。法的効力の保証を目的とするものではありません。重要な判断には専門家へのご相談をお勧めします。")

        c.setFillColor(colors.HexColor("#cbd5e1"))
        c.drawRightString(PAGE_W - ML, foot_top - 6*mm, f"© 2026 Imprint  |  IMP-{cert_no}")

        c.restoreState()

    # ── パラグラフスタイル定義 ──
    def s_h2():
        return ParagraphStyle("h2", fontName=JA, fontSize=9.5, leading=14,
                              textColor=NAVY, spaceBefore=0, spaceAfter=3)

    def s_body():
        return ParagraphStyle("body", fontName=JA_SERIF, fontSize=8, leading=13,
                              textColor=SLATE, spaceAfter=3)

    # ── セクションヘッダー（左インディゴバー付き） ──
    def section_header(label: str):
        tbl = Table(
            [["", Paragraph(label, s_h2())]],
            colWidths=[3*mm, CW - 3*mm],
            rowHeights=[9*mm],
        )
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), LIGHT),
            ("BACKGROUND",    (0, 0), (0,  0),  INDIGO),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (1, 0), (1,  0),  8),
            ("LEFTPADDING",   (0, 0), (0,  0),  0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
        ]))
        return tbl

    # ── KVテーブル ──
    def kv_table(rows: list):
        col1 = 50*mm
        table = Table(rows, colWidths=[col1, CW - col1])
        table.setStyle(TableStyle([
            ("FONTNAME",      (0, 0), (0, -1), JA),
            ("FONTNAME",      (1, 0), (1, -1), JA_SERIF),
            ("FONTSIZE",      (0, 0), (-1, -1), 8.5),
            ("LEADING",       (0, 0), (-1, -1), 13),
            ("TEXTCOLOR",     (0, 0), (0, -1), SLATE),
            ("TEXTCOLOR",     (1, 0), (1, -1), NAVY),
            ("ROWBACKGROUNDS",(0, 0), (-1, -1), [WHITE, LIGHT]),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
            ("BOX",           (0, 0), (-1, -1), 0.4, BORDER),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.3, BORDER),
        ]))
        return table

    # ── コンテンツ組み立て ──
    story = []
    SP = Spacer(1, 3*mm)

    # ファイル情報
    f = verify_result["file"]
    story.append(section_header("ファイル情報"))
    story.append(kv_table([
        ["ファイル名",     f["filename"]],
        ["フォーマット",   f"{f['format']} ({f['content_type']})"],
        ["解　像　度",     f"{f['width']} × {f['height']} px"],
        ["ファイルサイズ", f"{f['size_bytes']:,} bytes（{f['size_bytes']/1024/1024:.2f} MB）"],
        ["検 証 日 時",    verify_result["verified_at"]],
    ]))
    story.append(SP)

    # SHA-256
    story.append(section_header("電子指紋（SHA-256 ハッシュ値）"))
    hash_val = verify_result["hash"]["value"]
    story.append(Paragraph(
        f'<font name="Courier" size="7.5" color="#334155">{hash_val}</font>',
        ParagraphStyle("hash_p", fontName=JA, fontSize=8, leading=13,
                       backColor=colors.HexColor("#f1f5f9"),
                       borderPadding=(5, 8, 5, 8), spaceAfter=2),
    ))
    story.append(Paragraph(
        "1ピクセルでも改変されると全く異なる値になります。この写真固有の64文字の識別子です。",
        s_body(),
    ))
    story.append(SP)

    # EXIF
    exif = verify_result["exif"]
    story.append(section_header("カメラ・撮影情報（EXIFメタデータ）"))
    gps_str = (f"{exif['gps_latitude']:.6f}, {exif['gps_longitude']:.6f}"
               if exif.get("gps_latitude") else "未記録")
    story.append(kv_table([
        ["EXIFデータ",    "記録あり" if exif["has_exif"] else "記録なし"],
        ["カ　メ　ラ",   (f"{exif.get('camera_make','')}"
                          f" {exif.get('camera_model','')}").strip() or "—"],
        ["撮 影 日 時",   exif.get("datetime_original") or "—"],
        ["焦 点 距 離",   f"{exif['focal_length']} mm" if exif.get("focal_length") else "—"],
        ["ISO 感 度",     str(exif["iso"]) if exif.get("iso") else "—"],
        ["GPS 座 標",     gps_str],
        ["ソフトウェア",  exif.get("software") or "—"],
        ["ソフト種別",    exif.get("software_category", "—")],
    ]))
    story.append(SP)

    # ELA
    ela = verify_result["ela"]
    ELA_JP = {
        "clean":        "加工なし（クリーン）",
        "suspicious":   "要注意（一部に異常パターンあり）",
        "likely_edited":"加工・改ざんの疑いあり",
        "unknown":      "判定不能",
        "error":        "解析エラー",
    }
    story.append(section_header("加工・改ざん解析（Error Level Analysis）"))
    story.append(kv_table([
        ["判　　定",          ELA_JP.get(ela["ela_verdict"], ela["ela_verdict"])],
        ["平均誤差（Mean）",  str(ela["ela_mean_diff"])],
        ["最大誤差（Max）",   str(ela["ela_max_diff"])],
        ["不自然ピクセル比",  f"{ela['ela_suspicious_ratio']:.2%}"],
    ]))
    story.append(SP)

    # AI検出
    ai = verify_result.get("ai_detection")
    if ai:
        AI_JP = {
            "ai_generated": "AI生成画像（高確率）",
            "suspicious":   "AI生成の疑いあり",
            "likely_real":  "本物の写真（AI生成でない）",
            "unknown":      "判定不能",
        }
        story.append(section_header("AI生成画像検査"))
        ai_rows = [
            ["判　　定",   AI_JP.get(ai.get("verdict", "unknown"), "不明")],
            ["検 出 方 法", ai.get("method") or "—"],
            ["詳　　細",   ai.get("detail") or "—"],
        ]
        if ai.get("ai_score") is not None:
            ai_rows += [
                ["AI生成スコア", f"{ai['ai_score']:.1%}"],
                ["実写スコア",   f"{ai['real_score']:.1%}"],
            ]
        story.append(kv_table(ai_rows))
        story.append(SP)

    # RFC 3161
    ts = verify_result.get("timestamp")
    if ts:
        story.append(section_header("RFC 3161 タイムスタンプ認証"))
        story.append(kv_table([
            ["ス テ ー タ ス",  "認定済み  ✓"],
            ["タイムスタンプ局", ts["tsa"]],
            ["認定日時（UTC）",  ts["tsa_time"]],
            ["シリアル番号",     ts.get("serial_no") or "—"],
            ["取 得 日 時",      ts["requested_at"]],
        ]))
        story.append(Paragraph(
            "本写真は RFC 3161 国際標準に準拠した認定タイムスタンプ局の電子署名により、"
            "上記日時に存在したことが第三者機関によって証明されています。",
            s_body(),
        ))
        story.append(SP)

    # スコア内訳
    if auth.get("deductions") or auth.get("details"):
        story.append(section_header("真正性スコア算出内訳"))
        rows = []
        for d in auth.get("deductions", []):
            rows.append([
                f'▼{abs(d["penalty"])}点',
                f'{d["factor"]} — {d["reason"]}',
            ])
        for d in auth.get("details", []):
            rows.append(["✓", d])
        tbl = Table(rows, colWidths=[18*mm, CW - 18*mm])
        tbl.setStyle(TableStyle([
            ("FONTNAME",      (0, 0), (-1, -1), JA),
            ("FONTSIZE",      (0, 0), (-1, -1), 8),
            ("LEADING",       (0, 0), (-1, -1), 12),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("TEXTCOLOR",     (0, 0), (0, -1), colors.HexColor("#dc2626")),
            ("TEXTCOLOR",     (1, 0), (1, -1), colors.HexColor("#374151")),
            ("ROWBACKGROUNDS",(0, 0), (-1, -1), [WHITE, LIGHT]),
            ("BOX",           (0, 0), (-1, -1), 0.4, BORDER),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.3, BORDER),
        ]))
        # 加点行（✓）を緑に
        for i, d in enumerate(auth.get("deductions", [])):
            pass  # 減点は赤（デフォルト）
        for i, _ in enumerate(auth.get("details", [])):
            row_idx = len(auth.get("deductions", [])) + i
            tbl.setStyle(TableStyle([
                ("TEXTCOLOR", (0, row_idx), (0, row_idx), colors.HexColor("#16a34a")),
            ]))
        story.append(tbl)

    # ── ドキュメント生成 ──
    TOP_M = BORDER_M + HEADER_H + SCORE_H + 5*mm
    BOT_M = BORDER_M + FOOT_H + 5*mm

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=ML, rightMargin=ML,
        topMargin=TOP_M, bottomMargin=BOT_M,
    )
    doc.build(story, onFirstPage=draw_page_decoration, onLaterPages=draw_page_decoration)
    buf.seek(0)
    return buf.read()


@app.post("/verify", summary="写真の真正性を検証する", tags=["Verification"])
async def verify_image(
    file: UploadFile = File(...),
    _key_id: str = Depends(require_api_key_limited),
    x_api_key: str = Depends(_api_key_header),
):
    """
    画像をアップロードして真正性を検証します。

    Returns:
    - **hash**: SHA-256ハッシュ（改ざん検知の基準値）
    - **exif**: EXIFメタデータ
    - **ela**: Error Level Analysis結果
    - **authenticity**: 総合真正性スコア（0〜100）
    """
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/tiff"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"対応フォーマット: JPEG, PNG, WebP, TIFF。受信: {file.content_type}"
        )

    raw_bytes = await file.read()
    file_size = len(raw_bytes)

    if file_size > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="ファイルサイズは20MB以下にしてください")

    try:
        image = Image.open(io.BytesIO(raw_bytes))
    except Exception:
        raise HTTPException(status_code=400, detail="画像の読み込みに失敗しました")

    sha256_hash  = compute_sha256(raw_bytes)
    exif_data    = extract_exif(image)
    ela_data     = ela_analysis(image)
    ai_data      = await detect_ai_generated(raw_bytes, image)
    ts_info      = _query_timestamp(sha256_hash)
    authenticity = compute_authenticity_score(exif_data, ela_data, file_size, ai_data, ts=ts_info)

    ts_response = (
        {**ts_info, "verify_url": f"/timestamp/verify/{sha256_hash}"} if ts_info else None
    )

    # 解析履歴を保存（ユーザー紐付き API キーの場合のみ）
    if x_api_key:
        _conn = _db()
        try:
            _uid_row = _conn.execute(
                "SELECT user_id FROM api_keys WHERE key_hash = ? AND is_active = 1",
                (_hash_key(x_api_key),),
            ).fetchone()
            if _uid_row and _uid_row["user_id"]:
                _uid = _uid_row["user_id"]
                _conn.execute(
                    """INSERT INTO analyses
                       (id, user_id, filename, file_size, file_hash, score, verdict,
                        ela_verdict, ai_verdict, has_timestamp, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, file_hash) DO UPDATE SET
                           filename=excluded.filename, file_size=excluded.file_size,
                           score=excluded.score, verdict=excluded.verdict,
                           ela_verdict=excluded.ela_verdict, ai_verdict=excluded.ai_verdict,
                           has_timestamp=excluded.has_timestamp,
                           created_at=excluded.created_at""",
                    (
                        secrets.token_hex(8), _uid, file.filename, file_size, sha256_hash,
                        authenticity["score"], authenticity["verdict"],
                        ela_data.get("ela_verdict"), ai_data.get("verdict"),
                        1 if ts_info else 0,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                _conn.commit()
        finally:
            _conn.close()

    return {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "file": {
            "filename": file.filename,
            "content_type": file.content_type,
            "size_bytes": file_size,
            "width": image.width,
            "height": image.height,
            "format": image.format,
        },
        "hash": {
            "algorithm": "SHA-256",
            "value": sha256_hash,
            "note": "このハッシュ値をブロックチェーンに記録することで改ざん検知が可能",
        },
        "exif": exif_data,
        "ela": ela_data,
        "ai_detection": ai_data,
        "authenticity": authenticity,
        "timestamp": ts_response,
        "certificate_url": f"/certificate?hash={sha256_hash}",
    }


@app.post("/certificate", summary="証明書PDFを発行する", tags=["Verification"])
async def issue_certificate(
    file: UploadFile = File(...),
    _key_id: str = Depends(require_api_key_limited),
):
    """
    画像を検証し、結果をPDF証明書として発行します。
    """
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/tiff"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="対応フォーマット: JPEG, PNG, WebP, TIFF")

    raw_bytes = await file.read()
    file_size = len(raw_bytes)

    if file_size > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="ファイルサイズは20MB以下にしてください")

    try:
        image = Image.open(io.BytesIO(raw_bytes))
    except Exception:
        raise HTTPException(status_code=400, detail="画像の読み込みに失敗しました")

    sha256_hash  = compute_sha256(raw_bytes)
    exif_data    = extract_exif(image)
    ela_data     = ela_analysis(image)
    ai_data      = await detect_ai_generated(raw_bytes, image)
    ts_info      = _query_timestamp(sha256_hash)
    authenticity = compute_authenticity_score(exif_data, ela_data, file_size, ai_data, ts=ts_info)

    verify_result = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "file": {
            "filename": file.filename,
            "content_type": file.content_type,
            "size_bytes": file_size,
            "width": image.width,
            "height": image.height,
            "format": image.format,
        },
        "hash": {"algorithm": "SHA-256", "value": sha256_hash},
        "exif": exif_data,
        "ela": ela_data,
        "ai_detection": ai_data,
        "authenticity": authenticity,
        "timestamp": ts_info,
    }

    pdf_bytes = build_certificate_pdf(verify_result)
    filename = f"imprint_certificate_{sha256_hash[:12]}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/hash", summary="SHA-256ハッシュのみ取得（軽量版）", tags=["Verification"])
async def get_hash_only(
    file: UploadFile = File(...),
    _key_id: str = Depends(require_api_key_limited),
):
    """画像のSHA-256ハッシュのみを高速に返します"""
    raw_bytes = await file.read()
    return {
        "filename": file.filename,
        "sha256": compute_sha256(raw_bytes),
        "size_bytes": len(raw_bytes),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
