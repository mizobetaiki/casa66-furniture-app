"""
判定パイプライン:
  - Claudeエンジン : Claude vision + web_search で裏取り
  - Geminiエンジン : Cloud Vision Web Detection → ページ情報抽出 → Gemini 統合判定

注: google 系 / anthropic 系のライブラリは、選んだエンジンで必要になった時点で
    遅延importする（片方のキーしか無くてもアプリが起動できるように）。
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import yaml
from PIL import Image

GEMINI_MODEL_NAME = "gemini-2.5-flash"
# Claude Sonnet 5（2026-07時点の現行Sonnet系モデルID。claude-apiスキルで確認済み）。
# Sonnet 4.6 からの主なメリット: 高解像度Vision(2576px)でロゴ・タグ・型番の読み取り精度が上がる。
CLAUDE_MODEL_NAME = "claude-sonnet-5"

# ─────────────────────────────────────────────────────────────
# 信頼ドメイン
# ─────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).parent
_TRUSTED_DOMAINS_PATH = _THIS_DIR / "reference" / "trusted_domains.yaml"


def _load_trusted_domains() -> dict[str, list[str]]:
    if not _TRUSTED_DOMAINS_PATH.exists():
        return {"official": [], "authorized_retailer": [], "marketplace": []}
    with _TRUSTED_DOMAINS_PATH.open("r", encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def classify_domain(url: str, trusted: dict[str, list[str]]) -> str:
    """URL を 'official' / 'authorized_retailer' / 'marketplace' / 'other' に分類"""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "other"
    for category in ("official", "authorized_retailer", "marketplace"):
        for d in trusted.get(category, []):
            if d in host:
                return category
    return "other"


# ─────────────────────────────────────────────────────────────
# データクラス
# ─────────────────────────────────────────────────────────────
@dataclass
class PageRef:
    url: str
    title: str = ""
    score: float = 0.0
    domain_class: str = "other"  # official / authorized_retailer / marketplace / other


@dataclass
class VisionContext:
    web_entities: list[dict] = field(default_factory=list)  # [{"description": "...", "score": 0.9}]
    best_guess_labels: list[str] = field(default_factory=list)
    pages: list[PageRef] = field(default_factory=list)
    full_matching_images: list[str] = field(default_factory=list)
    partial_matching_images: list[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        """Gemini に渡しやすい形式に整形"""
        lines = []
        if self.best_guess_labels:
            lines.append(f"Google画像逆引きの推定ラベル: {', '.join(self.best_guess_labels)}")
        if self.web_entities:
            top = sorted(self.web_entities, key=lambda e: e.get("score", 0), reverse=True)[:10]
            lines.append("画像から検出されたエンティティ候補（信頼度順）:")
            for e in top:
                desc = e.get("description") or "(無名)"
                lines.append(f"  - {desc} (score={e.get('score', 0):.2f})")
        if self.pages:
            lines.append("類似画像が掲載されているWebページ（公式・正規店優先）:")
            sorted_pages = sorted(
                self.pages,
                key=lambda p: (
                    {"official": 0, "authorized_retailer": 1, "marketplace": 2, "other": 3}.get(p.domain_class, 4),
                    -p.score,
                ),
            )
            for p in sorted_pages[:10]:
                lines.append(f"  - [{p.domain_class}] {p.title} | {p.url}")
        return "\n".join(lines) if lines else "（Vision検出結果なし）"


# ─────────────────────────────────────────────────────────────
# Step 1: Cloud Vision Web Detection
# ─────────────────────────────────────────────────────────────
def detect_with_vision(image_bytes_list: list[bytes]) -> VisionContext:
    """複数画像をCloud Visionで逆引きし、結果をマージして返す"""
    from google.cloud import vision  # 遅延import

    client = vision.ImageAnnotatorClient()
    trusted = _load_trusted_domains()

    merged = VisionContext()
    seen_pages: dict[str, PageRef] = {}
    seen_entities: dict[str, dict] = {}

    for img_bytes in image_bytes_list:
        image = vision.Image(content=img_bytes)
        response = client.web_detection(image=image, max_results=10)
        if response.error.message:
            raise RuntimeError(f"Cloud Vision error: {response.error.message}")
        wd = response.web_detection

        for lbl in wd.best_guess_labels:
            if lbl.label and lbl.label not in merged.best_guess_labels:
                merged.best_guess_labels.append(lbl.label)

        for ent in wd.web_entities:
            desc = ent.description or ""
            if not desc:
                continue
            # 同じエンティティが複数画像で出たらscoreを最大値で更新
            if desc not in seen_entities or seen_entities[desc]["score"] < ent.score:
                seen_entities[desc] = {"description": desc, "score": float(ent.score)}

        for page in wd.pages_with_matching_images:
            url = page.url or ""
            if not url or url in seen_pages:
                continue
            score = float(page.score) if page.score else 0.0
            seen_pages[url] = PageRef(
                url=url,
                title=page.page_title or "",
                score=score,
                domain_class=classify_domain(url, trusted),
            )

        for img in wd.full_matching_images[:5]:
            if img.url and img.url not in merged.full_matching_images:
                merged.full_matching_images.append(img.url)
        for img in wd.partial_matching_images[:5]:
            if img.url and img.url not in merged.partial_matching_images:
                merged.partial_matching_images.append(img.url)

    merged.web_entities = list(seen_entities.values())
    merged.pages = list(seen_pages.values())
    return merged


# ─────────────────────────────────────────────────────────────
# Step 3: Gemini 統合判定
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """あなたは中古家具の査定士です。casa66（中古家具引取・販売）の担当者が
撮影した写真と、Google画像逆引き（Cloud Vision Web Detection）の結果を組み合わせて、
ブランド・定価・査定額を判定してください。

【最重要ルール】
1. ブランド名・商品名・定価は、以下のいずれかの「物証」を根拠にする：
   - 写真内に視認できるロゴ・タグ・刻印・型番
   - Cloud Visionが返した類似画像ページ（特に公式・正規店ドメイン）
   「雰囲気が似ている」だけで有名ブランド・有名商品名を答えてはいけません。物証がない場合は
   brand や product_name を "不明"/null にしてください。
2. 信頼度（confidence）が 0.7 未満なら brand を "不明" にしてください。
3. 定価は、採用したページのURLを必ず evidence_urls に明示してください。
   URL根拠が取れない定価は null を返すこと（推測で数字を作らない）。
4. 公式（official）> 正規店（authorized_retailer）> マーケットプレイス（marketplace）> その他 の順で信頼度を上げる。
   marketplace（メルカリ等）のページは「中古相場」の参考にはなるが「定価」の根拠にはしない。
5. 査定額は定価が分かる場合のみ算出。不明なら null。
6. 写真と家具種別（ソファ/ダイニングテーブル/チェア等）から、配送方法の判定に使う
   「推定寸法（幅・奥行・高さ cm）」と「推定重量（kg）」も可能な範囲で見積もる。
   写真だけでは自信を持って言えない場合は無理に数値を作らず null にすること
   （ユーザーが後で実寸を手入力できるため、null で構わない）。
7. product_name（一般に通じる商品名・シリーズ名）を返す。ブランド（メーカー名）や
   model_name（型番・品番）とは別物。例：ブランド="カリモク60"／product_name="Kチェア ロビーチェア2シーター"／
   model_name="品番があればそれ"。物証（ロゴ・タグ・Web検索の裏取り）が無ければ product_name は null にする
   （雰囲気で有名商品名を断定しない）。

【出力フォーマット】必ず以下のJSONのみを返す（前後の説明文・コードブロック禁止）:
{
  "candidates": [
    {
      "brand": "ブランド名（メーカー名）or 不明",
      "product_name": "一般に通じる商品名・シリーズ名 or null",
      "model_name": "型番・品番 or null",
      "confidence": 0.0-1.0,
      "evidence": "判定根拠（見えたロゴ・タグ・Vision結果の何を採用したか具体的に）",
      "evidence_urls": ["採用したページURL", ...],
      "list_price_jpy": 定価の数値 or null,
      "list_price_source_url": "定価を確認したページURL or null"
    }
  ],
  "furniture_type": "ソファ / ダイニングチェア / 学習机 etc",
  "material": "オーク無垢 / ファブリック etc",
  "condition_notes": "傷・汚れ・年代感の所感",
  "estimated_dimensions_cm": {
    "width": 推定の幅(cm) or null,
    "depth": 推定の奥行(cm) or null,
    "height": 推定の高さ(cm) or null
  },
  "estimated_weight_kg": 推定重量(kg) or null,
  "valuation": {
    "kaitori_jpy_low": 業者買取下限 or null,
    "kaitori_jpy_high": 業者買取上限 or null,
    "mercari_jpy_low": メルカリ販売下限 or null,
    "mercari_jpy_high": メルカリ販売上限 or null,
    "recommended_channel": "業者買取 / メルカリ / ジモティー / 廃棄推奨",
    "reasoning": "なぜこのチャネルがおすすめか1〜2行"
  },
  "photo_quality_feedback": "もっと精度を上げるために追加で撮ってほしい写真の指示"
}
"""

USER_PROMPT_TEMPLATE = """添付の写真は、お客様から無料引き取りした中古家具です。
ブランド・定価・査定額を判定してください。

家具の追加情報（任意）:
{notes}

【Cloud Vision Web Detection の結果】
{vision_block}

上記のVision結果と写真の両方を参照し、ルールに従ってJSONのみを返してください。
"""


def judge_with_gemini(
    images: list[Image.Image],
    vision_ctx: VisionContext,
    notes: str,
    api_key: str,
) -> dict:
    import google.generativeai as genai  # 遅延import

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL_NAME, system_instruction=SYSTEM_PROMPT)

    user_prompt = USER_PROMPT_TEMPLATE.format(
        notes=notes or "（なし）",
        vision_block=vision_ctx.to_prompt_block(),
    )
    parts = [user_prompt] + images

    response = model.generate_content(
        parts,
        generation_config={
            "temperature": 0.2,
            "response_mime_type": "application/json",
        },
    )
    raw = response.text.strip()
    return _extract_json(raw)


def _extract_json(text: str) -> dict:
    """LLM応答テキストからJSON部分を取り出して dict にする（コードブロック等を除去）"""
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    # 先頭・末尾に説明文が混ざっていても最初の { 〜 最後の } を拾う
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1]
    return json.loads(raw)


# ─────────────────────────────────────────────────────────────
# Claude エンジン（vision + web_search で裏取り）
# ─────────────────────────────────────────────────────────────
CLAUDE_SYSTEM_PROMPT = SYSTEM_PROMPT.replace(
    "撮影した写真と、Google画像逆引き（Cloud Vision Web Detection）の結果を組み合わせて、",
    "撮影した写真を見て、必要に応じて web_search ツールでブランド名・型番をWeb検索し、",
).replace(
    "   - Cloud Visionが返した類似画像ページ（特に公式・正規店ドメイン）",
    "   - web_search で見つけた公式・正規店ページ（型番やロゴ名で検索して裏取りする）",
) + """

【web_search の使い方（重要）】
- 写真からロゴ・タグ・型番が読めたら、その文字列で web_search を実行し、
  メーカー公式サイトや正規販売店ページを探して「定価」を裏取りすること。
- 見つけた公式/正規店ページのURLを evidence_urls と list_price_source_url に必ず記載する。
- メルカリ・ジモティー等のフリマ相場は「中古相場(mercari_jpy_*)」の参考に使い、「定価」の根拠にはしない。
- 検索しても確証が得られない場合は、推測せず brand を "不明"、定価を null にする。
"""

CLAUDE_USER_PROMPT_TEMPLATE = """添付の写真は、お客様から無料引き取りした中古家具です。
ブランド・定価・査定額を判定してください。

家具の追加情報（任意）:
{notes}

写真からロゴ・タグ・型番が読み取れたら web_search で裏取りし、ルールに従ってJSONのみを返してください。
"""


def judge_with_claude(
    images: list[Image.Image],
    notes: str,
    api_key: str,
    max_continuations: int = 5,
) -> dict:
    """Claude vision + web_search でブランド・定価・査定額を判定する"""
    import anthropic  # 遅延import

    # 502/529等の一時的なサーバー混雑に備えて自動リトライを多めに、タイムアウトも長めに
    client = anthropic.Anthropic(api_key=api_key, max_retries=5, timeout=120.0)

    # 画像を base64(JPEG) にして content ブロックを組み立てる
    content_blocks: list[dict] = []
    for img in images:
        buf = BytesIO()
        rgb = img if img.mode == "RGB" else img.convert("RGB")
        rgb.save(buf, format="JPEG", quality=85)
        b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
        content_blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            }
        )
    content_blocks.append(
        {"type": "text", "text": CLAUDE_USER_PROMPT_TEMPLATE.format(notes=notes or "（なし）")}
    )

    messages = [{"role": "user", "content": content_blocks}]
    # web_search_20260209: 動的フィルタリング対応の現行版（claude-apiスキルで確認済み。
    # claude-sonnet-5 は対応モデルに含まれる）
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
    system = [
        {"type": "text", "text": CLAUDE_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]

    response = None
    try:
        for _ in range(max_continuations):
            response = client.messages.create(
                model=CLAUDE_MODEL_NAME,
                max_tokens=4096,
                system=system,
                tools=tools,
                messages=messages,
            )
            # web_search はサーバー側ループのため pause_turn で返ることがある → 続行
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            break
    except anthropic.APIStatusError as e:
        status = getattr(e, "status_code", None)
        if status in (500, 502, 503, 504, 529):
            raise RuntimeError(
                "Anthropicのサーバーが一時的に混み合っています（リトライしても回復しませんでした）。"
                "1〜2分ほど待ってからもう一度「査定する」を押してください。"
            ) from e
        if status == 401:
            raise RuntimeError(
                "APIキーが正しくないようです。ANTHROPIC_API_KEY を確認して再起動してください。"
            ) from e
        if status == 400:
            raise RuntimeError(f"リクエストエラー: {getattr(e, 'message', str(e))}") from e
        raise RuntimeError(f"Claude APIエラー（{status}）。少し待って再試行してください。") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError(
            "ネットワークに接続できませんでした。Wi-Fi接続を確認して再試行してください。"
        ) from e

    # 応答テキスト（text ブロック）を結合
    text_out = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    return _extract_json(text_out)


# ─────────────────────────────────────────────────────────────
# 統合エントリポイント
# ─────────────────────────────────────────────────────────────
def run_pipeline(
    pil_images: list[Image.Image],
    image_bytes_list: list[bytes],
    notes: str,
    engine: str = "claude",
    gemini_api_key: str = "",
    anthropic_api_key: str = "",
    skip_vision: bool = False,
) -> tuple[dict, VisionContext]:
    """
    Args:
        engine: "claude" または "gemini"
    Returns:
        (verdict_json, vision_context) のタプル
        ※ Claudeエンジンでは vision_context は空（Visionを使わないため）
    """
    if engine == "claude":
        verdict = judge_with_claude(pil_images, notes, anthropic_api_key)
        return verdict, VisionContext()

    # gemini エンジン
    if skip_vision:
        vision_ctx = VisionContext()
    else:
        vision_ctx = detect_with_vision(image_bytes_list)
    verdict = judge_with_gemini(pil_images, vision_ctx, notes, gemini_api_key)
    return verdict, vision_ctx


# ─────────────────────────────────────────────────────────────
# 着払い可否判定（ゆうパック等の「3辺合計160サイズ」基準）
# ─────────────────────────────────────────────────────────────
SHIPPING_SIZE_LIMIT_CM = 160.0
SHIPPING_WEIGHT_LIMIT_KG = 25.0
# このcm範囲内（境界付近）は誤差で判定が変わりうるので「要実測」を促す
SHIPPING_BOUNDARY_LOW_CM = 150.0
SHIPPING_BOUNDARY_HIGH_CM = 170.0


def classify_shipping(
    width_cm: float | None,
    depth_cm: float | None,
    height_cm: float | None,
    weight_kg: float | None,
) -> dict:
    """
    幅・奥行・高さの3辺合計と重量から「小型版（引き取り基本＋着払い発送あり）」か
    「大型版（引き取り限定）」かを判定する。

    判定基準: 3辺（幅+奥行+高さ）の合計が160cm以内、かつ重量が25kg以内 → 小型版
    （ゆうパック等の「3辺合計160サイズ」基準に準拠）

    Returns:
        {
          "size_class": "small" | "large" | "unknown",
          "cod_allowed": bool | None,   # 着払い発送に対応できるか
          "total_cm": float | None,     # 3辺合計（参考値）
          "reason": str,                # 判定理由（人間が読む用）
          "needs_measurement_check": bool,  # 境界付近 or 未確定で実測を促すべきか
        }
    """
    if width_cm is None or depth_cm is None or height_cm is None or weight_kg is None:
        return {
            "size_class": "unknown",
            "cod_allowed": None,
            "total_cm": None,
            "reason": "寸法・重量の一部が未確定のため判定できません。実寸を入力してください。",
            "needs_measurement_check": True,
        }

    total_cm = float(width_cm) + float(depth_cm) + float(height_cm)
    weight_kg = float(weight_kg)
    is_small = total_cm <= SHIPPING_SIZE_LIMIT_CM and weight_kg <= SHIPPING_WEIGHT_LIMIT_KG
    near_boundary = SHIPPING_BOUNDARY_LOW_CM <= total_cm <= SHIPPING_BOUNDARY_HIGH_CM

    if is_small:
        reason = (
            f"3辺合計 {total_cm:.0f}cm（160cm以内）・重量 {weight_kg:.1f}kg（25kg以内）のため"
            "小型版（引き取り基本＋着払い発送あり）と判定しました。"
        )
    else:
        over_points = []
        if total_cm > SHIPPING_SIZE_LIMIT_CM:
            over_points.append(f"3辺合計 {total_cm:.0f}cm（160cm超）")
        if weight_kg > SHIPPING_WEIGHT_LIMIT_KG:
            over_points.append(f"重量 {weight_kg:.1f}kg（25kg超）")
        reason = "・".join(over_points) + " のため大型版（引き取り限定）と判定しました。"

    if near_boundary:
        reason += "（3辺合計が150〜170cmの境界付近のため、実測での確認を推奨します）"

    return {
        "size_class": "small" if is_small else "large",
        "cod_allowed": is_small,
        "total_cm": total_cm,
        "reason": reason,
        "needs_measurement_check": near_boundary,
    }


# ─────────────────────────────────────────────────────────────
# 出品説明文テンプレート（「安心・誠実型」・石川さん検証済みパターン）
# ─────────────────────────────────────────────────────────────
_LISTING_HEADER = {
    "small": "【引き取り基本｜渋谷・富ヶ谷】",
    "large": "【引き取り限定｜渋谷・富ヶ谷】",
}

_LISTING_OPENING = (
    "美品です。気になる点（脚部に小傷1か所）は写真【◯枚目】に正直に載せています。清掃・検品済み。"
)

_MERCARI_BODY = (
    "中古品ですが、引き上げ後にしっかりクリーニング。状態の不安が出やすい中古家具だからこそ、"
    "傷もニオイも隠さず写真で全部お見せしています。"
)
_MERCARI_BULLETS = {
    "small": [
        "・渋谷・富ヶ谷での引き取りが基本です（その場でご確認いただけます）",
        "・小型のため、遠方の方は着払い発送も対応可能です（送料はご購入者さま負担）",
        "・即購入OK、ご不明点はコメントでお気軽に",
    ],
    "large": [
        "・渋谷・富ヶ谷での引き取りが基本です（その場でご確認いただけます）",
        "・即購入OK、ご不明点はコメントでお気軽に",
    ],
}
_MERCARI_LARGE_NOTE = (
    "※メルカリは仕様上、大型はたのメル便発送が強制され引き取り不可。"
    "大型はジモティー／ヤフオク中心が無難です。"
)

_JIMOTY_BODY = (
    "中古ですが検品・清掃済みで、状態は写真の通り正直に出しています。"
    "実物の追加写真もご希望あればお送りします。"
)
_JIMOTY_BULLETS = {
    "small": [
        "・渋谷・富ヶ谷での引き取りが基本です（その場でご確認いただけます）",
        "・小型のため、遠方の方は着払い発送も対応可能です（送料はご購入者さま負担）",
        "・先着優先。取りに来られる方を歓迎します",
    ],
    "large": [
        "・渋谷・富ヶ谷での引き取りが基本です（その場でご確認いただけます）",
        "・先着優先。取りに来られる方を歓迎します",
    ],
}

_YAHOO_BODY = (
    "中古品です。状態は写真と記載の通りで、見えている小傷以外に大きなダメージはありません。"
    "判断材料として写真を多めに載せています。"
)
_YAHOO_BULLETS = {
    "small": [
        "・渋谷・富ヶ谷での引き取りが基本です",
        "・遠方の方は着払い発送も対応可能です（送料はご落札者さま負担）",
        "・中古品につきノークレーム・ノーリターンでお願いします。ご不明点は質問欄からどうぞ",
    ],
    "large": [
        "・渋谷・富ヶ谷での引き取りが基本です",
        "・中古品につきノークレーム・ノーリターンでお願いします。ご不明点は質問欄からどうぞ",
    ],
}

# チャネルの表示順もこの並びに揃える
LISTING_CHANNELS = {
    "mercari": {"label": "メルカリ", "body": _MERCARI_BODY, "bullets": _MERCARI_BULLETS, "large_note": _MERCARI_LARGE_NOTE},
    "jimoty": {"label": "ジモティー", "body": _JIMOTY_BODY, "bullets": _JIMOTY_BULLETS, "large_note": None},
    "yahoo": {"label": "ヤフオク", "body": _YAHOO_BODY, "bullets": _YAHOO_BULLETS, "large_note": None},
}


def _fmt_dim(value: float | None) -> str:
    """寸法値の表示整形。未確定なら「要入力」"""
    if value is None:
        return "要入力"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "要入力"
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


def compose_item_label(
    product_name: str | None,
    brand: str | None,
    model_name: str | None = None,
) -> str | None:
    """
    出品文・査定サマリー用の「商品名｜ブランド」ラベルを組み立てる。
    優先度: 商品名（product_name）> 型番（model_name）付きブランド > ブランド単体。
    物証が無い（brand="不明" / 全て空）場合は None を返し、テンプレ側で
    既定文言「商品名／ブランド」を使わせる（雰囲気で断定しないため）。
    """
    pn = product_name.strip() if product_name and product_name.strip() else None
    br = brand.strip() if brand and brand.strip() and brand.strip() != "不明" else None
    mn = model_name.strip() if model_name and model_name.strip() else None

    if pn and br:
        return f"{pn}｜{br}"
    if pn:
        return pn
    if br and mn:
        return f"{br} {mn}"
    if br:
        return br
    return None


def build_listing_text(
    channel: str,
    size_class: str,
    item_name: str | None,
    material: str | None,
    width_cm: float | None,
    depth_cm: float | None,
    height_cm: float | None,
) -> str:
    """
    「安心・誠実型」テンプレに沿って1チャネル分の出品説明文を組み立てる。
    傷写真番号【◯枚目】は自動で埋められないため、そのまま残す
    （UI側で「投稿前に差し替えてください」と案内する）。

    item_name には compose_item_label() が返す「商品名｜ブランド」等を渡す想定。
    """
    if channel not in LISTING_CHANNELS:
        raise ValueError(f"未知のチャネルです: {channel}")
    cfg = LISTING_CHANNELS[channel]
    size_key = "large" if size_class == "large" else "small"

    header = _LISTING_HEADER[size_key]
    name_label = item_name.strip() if item_name and item_name.strip() else "商品名／ブランド"
    material_label = material.strip() if material and material.strip() else "天然木"

    lines = [
        header,
        "",
        _LISTING_OPENING,
        "",
        f"【{name_label}】｜{material_label}",
        f"サイズ：幅【{_fmt_dim(width_cm)}】×奥行【{_fmt_dim(depth_cm)}】×高さ【{_fmt_dim(height_cm)}】cm",
        "",
        cfg["body"],
        "",
        *cfg["bullets"][size_key],
    ]

    if size_key == "large" and cfg["large_note"]:
        lines += ["", cfg["large_note"]]

    return "\n".join(lines)


def build_all_listings(
    size_class: str,
    item_name: str | None,
    material: str | None,
    width_cm: float | None,
    depth_cm: float | None,
    height_cm: float | None,
) -> dict:
    """3チャネル（メルカリ/ジモティー/ヤフオク）分の出品文をまとめて返す。key=channel名"""
    return {
        channel: build_listing_text(channel, size_class, item_name, material, width_cm, depth_cm, height_cm)
        for channel in LISTING_CHANNELS
    }
