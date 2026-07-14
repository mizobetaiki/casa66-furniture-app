"""
casa66 中古家具 AI査定アプリ（石川さん専用PoC）

判定エンジンは Claude 固定（2026-07・エンジン選択UIを撤去）:
  - Claude : Claude vision + web_search で裏取り（ANTHROPIC_API_KEY 1個でOK）
  ※ Gemini / Cloud Vision のコードは pipeline.py に残置（将来復活用）。app.py からは呼ばない。

かんたん起動（石川さん向け）:
    1. このフォルダに api_key.txt（キー1行）を置く
    2. run_furniture_app.bat（Win）/ .command（Mac）をダブルクリック

上級者向け（コマンド起動）:
    1. cd このフォルダ
    2. 初回のみ: pip install -r requirements.txt
    3. export ANTHROPIC_API_KEY="sk-ant-..."
    4. streamlit run app.py
"""

import csv
import os
import textwrap
from datetime import datetime
from io import BytesIO, StringIO

import streamlit as st
from PIL import Image


def html(markup: str) -> None:
    """字下げを除去してHTMLを描画（Markdownのコードブロック誤認を防ぐ）"""
    st.markdown(textwrap.dedent(markup).strip(), unsafe_allow_html=True)


def get_secret(name: str) -> str:
    """
    設定値をクラウド／ローカル両対応で読む。
      ① st.secrets（Streamlit Cloud の Secrets）
      ② os.environ（ローカル：run_furniture_app が api_key.txt からセット）
    ※ ローカルで secrets.toml が無いと st.secrets へのアクセスは例外を投げるため、
      try/except で必ず握りつぶして env にフォールバックする（ここが最大のバグ源）。
    """
    try:
        val = st.secrets.get(name)  # secrets.toml 未配置だと例外 → except へ
        if val:
            return str(val)
    except Exception:
        pass
    return os.environ.get(name, "")

try:
    from pipeline import (
        LISTING_CHANNELS,
        VisionContext,
        build_all_listings,
        classify_shipping,
        compose_item_label,
        run_pipeline,
    )
except ImportError as e:
    st.error(f"依存パッケージが未インストール: {e}\n`pip install -r requirements.txt` を実行してください。")
    st.stop()

# ─────────────────────────────────────────────────────────────
# 設定（クラウド=st.secrets ／ ローカル=環境変数 の両対応）
# ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY")
# Gemini/Cloud Vision は現在 app.py から呼ばない（Claude固定）が、将来復活用に env のみ参照
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_APP_CRED = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
# 合言葉（設定があるときだけゲートを出す。ローカルは未設定＝ゲートなしで従来UX）
APP_PASSCODE = get_secret("APP_PASSCODE")

st.set_page_config(page_title="casa66 | 中古家具 査定", page_icon="🪑", layout="centered")

# ─────────────────────────────────────────────────────────────
# デザイン（casa66ブランド：ミニマル・ラグジュアリー / 余白多め / 上質なサンセリフ）
# ─────────────────────────────────────────────────────────────
html(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@300;400;500;700&family=Cormorant+Garamond:wght@400;500;600&display=swap');
      :root {
        --ink: #1E1C19;
        --ink-soft: #6B665E;
        --line: #E4E0D7;
        --paper: #FBFAF7;
        --card: #FFFFFF;
        --accent: #1F1D1A;
        --sand: #B8A98C;
      }

      /* 全体タイポ */
      html, body, [class*="css"], .stApp, .stMarkdown, p, span, div, label {
        font-family: "Zen Kaku Gothic New", -apple-system, "Hiragino Sans", sans-serif;
        color: var(--ink);
        letter-spacing: .01em;
      }
      .stApp { background: var(--paper); }

      /* デフォルトのStreamlitヘッダー・余白を整理 */
      header[data-testid="stHeader"] { background: transparent; }
      .block-container { padding-top: 2.2rem; padding-bottom: 4rem; max-width: 760px; }

      /* ===== ヒーロー ===== */
      .c66-hero { padding: .5rem 0 1.6rem; border-bottom: 1px solid var(--line); margin-bottom: 2rem; }
      .c66-brand {
        font-family: "Cormorant Garamond", serif;
        font-size: 2.9rem; font-weight: 600; line-height: 1; letter-spacing: .02em;
        color: var(--ink); margin: 0;
      }
      .c66-brand .dot { color: var(--sand); }
      .c66-sub {
        margin: .7rem 0 0; font-size: .82rem; font-weight: 400; letter-spacing: .22em;
        text-transform: uppercase; color: var(--ink-soft);
      }
      .c66-lead { margin: 1.1rem 0 0; font-size: 1.02rem; font-weight: 400; color: var(--ink); }

      /* ===== セクション見出し ===== */
      .c66-eyebrow {
        font-size: .72rem; letter-spacing: .2em; text-transform: uppercase;
        color: var(--sand); font-weight: 700; margin: 0 0 .3rem;
      }

      /* 見出し（st.subheader等） */
      h1, h2, h3 { font-weight: 500 !important; letter-spacing: .02em; color: var(--ink) !important; }

      /* ===== ボタン ===== */
      .stButton > button {
        background: var(--accent); color: #FBFAF7; border: 1px solid var(--accent);
        border-radius: 2px; padding: .7rem 1.4rem; font-weight: 500; letter-spacing: .08em;
        transition: all .25s ease; box-shadow: none;
      }
      .stButton > button:hover:not(:disabled) {
        background: #FBFAF7; color: var(--accent); border-color: var(--accent);
      }
      .stButton > button:disabled { background: #D9D5CC; border-color: #D9D5CC; color: #fff; }

      /* ===== ファイルアップローダー ===== */
      [data-testid="stFileUploaderDropzone"] {
        background: var(--card); border: 1px dashed var(--line); border-radius: 4px;
      }

      /* ===== サイドバー ===== */
      section[data-testid="stSidebar"] { background: var(--card); border-right: 1px solid var(--line); }

      /* ===== カード（結果ブロック） ===== */
      .c66-card {
        background: var(--card); border: 1px solid var(--line); border-radius: 6px;
        padding: 1.6rem 1.8rem; margin: 1rem 0;
      }
      .c66-brandname {
        font-family: "Cormorant Garamond", serif; font-size: 2rem; font-weight: 600;
        line-height: 1.1; margin: .1rem 0 .2rem;
      }
      .c66-model { font-size: .9rem; color: var(--ink-soft); margin: 0 0 .8rem; letter-spacing: .04em; }
      .c66-price { font-family: "Cormorant Garamond", serif; font-size: 2.1rem; font-weight: 600; }
      .c66-price-label { font-size: .72rem; letter-spacing: .18em; text-transform: uppercase; color: var(--ink-soft); }

      /* メトリック */
      [data-testid="stMetricValue"] { font-family: "Cormorant Garamond", serif; font-weight: 600; }

      /* 区切り線を細く上品に */
      hr { border-color: var(--line); }

      /* リンク */
      a, a:visited { color: var(--accent); text-decoration: underline; text-underline-offset: 3px; }

      /* Streamlitのフッター・メニューを隠す */
      #MainMenu, footer { visibility: hidden; }

      /* ===== 横並びカード（スマホ幅では縦積みに） ===== */
      .c66-row { display: flex; gap: 1rem; }
      @media (max-width: 640px) {
        .c66-row { flex-direction: column; }
        .c66-brand { font-size: 2.2rem; }
        .block-container { padding-left: 1rem; padding-right: 1rem; }
      }

      /* ===== 出品文コピー枠 ===== */
      .c66-hint { font-size: .82rem; color: var(--ink-soft); margin: .2rem 0 .8rem; }

      /* ===== 査定サマリー（必ず出す4項目） ===== */
      .c66-summary {
        display: grid; grid-template-columns: 1fr 1fr; gap: 1.1rem 1.6rem;
        padding: 1.6rem 1.8rem;
      }
      .c66-sum-item { min-width: 0; }
      .c66-sum-label {
        font-size: .68rem; letter-spacing: .18em; text-transform: uppercase;
        color: var(--sand); font-weight: 700; margin: 0 0 .25rem;
      }
      .c66-sum-value {
        font-family: "Cormorant Garamond", serif; font-size: 1.55rem; font-weight: 600;
        line-height: 1.2; margin: 0; color: var(--ink);
      }
      .c66-sum-name {
        font-family: "Zen Kaku Gothic New", sans-serif; font-size: 1.12rem; font-weight: 500;
        word-break: break-word;
      }
      .c66-sum-dim { font-size: 1.15rem; }
      @media (max-width: 640px) {
        .c66-summary { grid-template-columns: 1fr; gap: .9rem; }
      }
    </style>
    """
)

html(
    """
    <div class="c66-hero">
      <p class="c66-brand">casa<span class="dot">.</span>66</p>
      <p class="c66-sub">Pre-owned Furniture Appraisal</p>
      <p class="c66-lead">写真をアップロードするだけで、ブランド・定価・買取/販売の相場を査定します。</p>
    </div>
    """
)


# ─────────────────────────────────────────────────────────────
# 合言葉（パスコード）ゲート
#   - APP_PASSCODE が設定されているとき（＝クラウド公開時）だけ出す。
#   - 未設定（＝ローカル運用）はゲートをスキップし従来どおり開く。
#   - パスコードの実値はコードにベタ書きせず secrets/env からのみ読む。
# ─────────────────────────────────────────────────────────────
def _passcode_gate() -> None:
    if not APP_PASSCODE:
        return  # ローカル運用：ゲートなし（UXを変えない）
    if st.session_state.get("authed"):
        return  # 認証済み

    st.markdown('<p class="c66-eyebrow" style="margin-top:1rem;">Locked</p>', unsafe_allow_html=True)
    html(
        """
        <div class="c66-card">
          <p style="font-size:1.05rem;font-weight:500;margin:0 0 .3rem;">合言葉を入力してください</p>
          <p style="color:var(--ink-soft);font-size:.88rem;margin:0;">
            このツールは関係者限定です。合言葉が分からない場合は、たいきさんに連絡してください。
          </p>
        </div>
        """
    )
    pw = st.text_input(
        "合言葉", type="password",
        label_visibility="collapsed", placeholder="合言葉を入力",
        key="passcode_input",
    )
    if st.button("確認", type="primary"):
        if pw and pw == APP_PASSCODE:
            st.session_state["authed"] = True
            st.rerun()  # ゲートUIを消して本体を表示
        else:
            st.error("合言葉が違います。")
    # 認証前は査定UIを一切描画しない
    st.stop()


_passcode_gate()

# ─────────────────────────────────────────────────────────────
# 判定エンジンは Claude 固定（2026-07・エンジン選択UIを撤去）
#   - Gemini/Cloud Vision のコードは pipeline.py に残してあるが、
#     app.py 側では呼ばない（engine="claude" 固定）。将来復活用。
#   - そのため環境変数チェックも ANTHROPIC_API_KEY のみを見る。
# ─────────────────────────────────────────────────────────────
engine = "claude"
skip_vision = False

# 環境変数チェック（Claude固定なので ANTHROPIC_API_KEY のみ）
missing = []
if not ANTHROPIC_API_KEY:
    missing.append("`ANTHROPIC_API_KEY`（Claude APIキー）")

if missing:
    st.error(
        "⚠️ APIキーが設定されていません。\n\n"
        "・ローカルで使う場合：このフォルダに `api_key.txt` を置いて起動ツール（run_furniture_app）を使ってください。\n"
        "・公開URL（Streamlit Cloud）で使う場合：管理者（たいきさん）に連絡してください（Secretsの設定が必要です）。\n\n"
        "詳しくは README.md を参照してください。"
    )

# ─────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────
st.markdown('<p class="c66-eyebrow">Upload</p>', unsafe_allow_html=True)

with st.expander("撮影のコツ（AI査定の精度がぐっと上がります）", expanded=False):
    st.markdown(
        """
        - **全体写真** 1枚（家具全体が入るように）
        - **ロゴ・タグ・刻印の近接写真** — 一番効きます。裏面・脚・引き出しの裏をチェック
        - **接合部・木目のアップ** 1枚（仕上げで上質さが分かります）
        - 明るい場所で、ピントを合わせて撮影
        """
    )

# ─────────────────────────────────────────────────────────────
# 撮影チェックリスト（チャネル別・売るための写真ガイド）
#   出典: members/ishikawa/ab-test-proposal/research-summary.md（2026-05-25 調査）
#   ※ ヤフオクは同ファイルに調査項目が無いため「一般的な中古出品の目安（研究データ外）」と明示。
#   チェック状態はその場のガイド用途（保存不要）。
# ─────────────────────────────────────────────────────────────
with st.expander("撮影チェックリスト（早く・高く売るための写真）", expanded=False):
    st.caption(
        "出典: 石川さんの写真研究 research-summary.md（2026-05-25調査）。"
        "撮りながらチェックに使えます（チェックは保存されません）。"
    )

    st.markdown("**共通5原則（3アプリ横断・出典: research-summary.md）**")
    common_principles = [
        "1枚目は全体が明るく鮮明な正面写真（背景はシンプルに）",
        "複数角度を網羅（正面・背面・側面・上面・脚部）",
        "傷・汚れは隠さずアップで提示（信頼獲得＆クレーム防止）",
        "寸法・型番・素材が分かるカットを必ず入れる",
        "設置イメージ（部屋に置いた引き写真）— 特にジモティーで効く",
    ]
    for i, item in enumerate(common_principles):
        st.checkbox(item, key=f"chk_common_{i}")

    st.divider()

    shoot_tab_mercari, shoot_tab_jimoty, shoot_tab_yahoo = st.tabs(
        ["メルカリ", "ジモティー", "ヤフオク"]
    )

    with shoot_tab_mercari:
        st.caption("出典: research-summary.md（メルカリ）")
        mercari_cuts = [
            "白〜淡色の無地背景で、正方形（1:1）のサムネを用意",
            "自然光で明るく、商品全体が中央に大きく映るように",
            "正面・背面・側面・裏面をそれぞれ撮る",
            "ブランドタグ・型番のアップ",
            "寸法メモ画像（サイズが一目で分かるカット）",
            "傷・汚れのアップ",
            "写真は最大20枚まで。多いほど売れやすい（過剰な加工はNG）",
        ]
        for i, item in enumerate(mercari_cuts):
            st.checkbox(item, key=f"chk_mercari_{i}")

    with shoot_tab_jimoty:
        st.caption("出典: research-summary.md（ジモティー）")
        jimoty_cuts = [
            "全体像（正面・引き）— 明るさ・清潔感・サイズ感が命",
            "メジャーを添えた寸法写真",
            "部屋に置いた引き写真（搬出・設置イメージ）",
            "メーカー・品番／傷汚れのカット",
            "本文に「搬出経路」と「解体可否」を明記する（写真は5枚まで）",
        ]
        for i, item in enumerate(jimoty_cuts):
            st.checkbox(item, key=f"chk_jimoty_{i}")

    with shoot_tab_yahoo:
        st.caption(
            "⚠️ ヤフオクは research-summary.md に調査項目がありません。"
            "以下は一般的な中古出品の目安（研究データ外・要検証）です。"
        )
        yahoo_cuts = [
            "状態が分かる写真を多めに載せる",
            "見えている傷以外に大きなダメージが無いことが分かるカット",
            "全体・複数角度・寸法・型番は共通5原則に準じて撮る",
        ]
        for i, item in enumerate(yahoo_cuts):
            st.checkbox(item, key=f"chk_yahoo_{i}")

uploaded = st.file_uploader(
    "家具の写真をアップロード（2〜4枚推奨）",
    type=["jpg", "jpeg", "png", "webp", "heic"],
    accept_multiple_files=True,
)

notes = st.text_area(
    "追加情報（任意）",
    placeholder="例: 引き取り元が『北欧家具屋で買った』と言っていた / ダイニングセットの椅子 など",
    height=80,
)

if uploaded:
    cols = st.columns(min(len(uploaded), 4))
    for i, f in enumerate(uploaded[:4]):
        with cols[i]:
            st.image(f, use_container_width=True)

st.session_state.setdefault("run_id", 0)

disabled = (not uploaded) or bool(missing)

if st.button("🔍 査定する", type="primary", disabled=disabled):
    if not uploaded:
        st.warning("写真をアップロードしてください。")
        st.stop()

    # 画像準備
    pil_images: list[Image.Image] = []
    bytes_list: list[bytes] = []
    for f in uploaded:
        raw_bytes = f.getvalue()
        img = Image.open(BytesIO(raw_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((1600, 1600))
        # PIL→JPEGバイト（Vision/Claude送信用に再圧縮）
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        bytes_list.append(buf.getvalue())
        pil_images.append(img)

    if engine == "claude":
        spinner_msg = "Claudeが査定中...（写真解析 → Web検索で裏取り／通常20〜40秒程度）"
    elif skip_vision:
        spinner_msg = "Geminiが査定中...（通常10〜20秒程度）"
    else:
        spinner_msg = "AIが査定中...（Vision逆引き → Gemini判定／通常20〜30秒程度）"

    with st.spinner(spinner_msg):
        try:
            verdict, vision_ctx = run_pipeline(
                pil_images=pil_images,
                image_bytes_list=bytes_list,
                notes=notes,
                engine=engine,
                gemini_api_key=GEMINI_API_KEY,
                anthropic_api_key=ANTHROPIC_API_KEY,
                skip_vision=skip_vision,
            )
        except Exception as e:
            st.error(
                f"⚠️ 査定中にエラーが発生しました。\n\n{e}\n\n"
                "解決しない場合は、お手数ですがたいきさんに連絡してください。"
            )
            st.stop()

    # 次のスクリプト再実行（寸法入力などのUI操作）でも結果が消えないよう保存しておく
    st.session_state["run_id"] += 1
    st.session_state["verdict"] = verdict
    st.session_state["vision_ctx"] = vision_ctx
    st.session_state["engine_used"] = engine
    st.session_state["skip_vision_used"] = skip_vision

# ─────────────────────────────────────────────────────────────
# 結果表示（ボタンを押した直後だけでなく、寸法入力などで画面を
# 再描画したあとも消えないよう、session_state から読み出す）
# ─────────────────────────────────────────────────────────────
if "verdict" in st.session_state:
    verdict = st.session_state["verdict"]
    vision_ctx = st.session_state["vision_ctx"]
    result_engine = st.session_state["engine_used"]
    result_skip_vision = st.session_state["skip_vision_used"]
    run_id = st.session_state["run_id"]

    st.markdown('<p class="c66-eyebrow" style="margin-top:2.4rem;">Appraisal Result</p>', unsafe_allow_html=True)

    candidates = verdict.get("candidates", [])
    top = candidates[0] if candidates else {}

    # ─────────────────────────────────────────────────────────
    # 査定サマリー（必ず出す4項目：商品名・ブランド・定価額・寸法）
    # 寸法は下の手入力欄（AI推定→ユーザー上書き）の値を反映する。
    # 手入力欄はこの下で描画されるため、値は session_state から先読みし、
    # まだ入力欄が無い初回は AI推定値にフォールバックする。
    # ─────────────────────────────────────────────────────────
    _est_dims_top = verdict.get("estimated_dimensions_cm") or {}

    def _dim_for_summary(state_key: str, est_val):
        v = st.session_state.get(state_key)
        if isinstance(v, (int, float)) and v > 0:
            return v
        return est_val if isinstance(est_val, (int, float)) and est_val > 0 else None

    _sum_w = _dim_for_summary(f"width_{run_id}", _est_dims_top.get("width"))
    _sum_d = _dim_for_summary(f"depth_{run_id}", _est_dims_top.get("depth"))
    _sum_h = _dim_for_summary(f"height_{run_id}", _est_dims_top.get("height"))

    def _dim_int(v):
        if not isinstance(v, (int, float)):
            return None
        return int(v) if float(v).is_integer() else v

    if _sum_w and _sum_d and _sum_h:
        _dim_text = f"幅{_dim_int(_sum_w)} × 奥行{_dim_int(_sum_d)} × 高さ{_dim_int(_sum_h)} cm"
    elif _sum_w or _sum_d or _sum_h:
        _dim_text = (
            f"幅{_dim_int(_sum_w) if _sum_w else '—'} × "
            f"奥行{_dim_int(_sum_d) if _sum_d else '—'} × "
            f"高さ{_dim_int(_sum_h) if _sum_h else '—'} cm（一部要確認）"
        )
    else:
        _dim_text = "要確認（下の欄で入力）"

    _sum_brand = top.get("brand") or ""
    _sum_brand_text = _sum_brand if _sum_brand and _sum_brand != "不明" else "—"
    _sum_product = top.get("product_name")
    _sum_product_text = _sum_product.strip() if _sum_product and str(_sum_product).strip() else "—"

    _sum_price = top.get("list_price_jpy")
    if _sum_price:
        _price_src = top.get("list_price_source_url")
        _price_link = (
            f'<a href="{_price_src}" target="_blank" style="font-size:.72rem;">出典 ↗</a>'
            if _price_src else ""
        )
        _sum_price_html = f'<span class="c66-sum-value">¥{_sum_price:,}</span> {_price_link}'
    else:
        _sum_price_html = '<span class="c66-sum-value">—</span>'

    html(
        f"""
        <div class="c66-card c66-summary">
          <div class="c66-sum-item">
            <p class="c66-sum-label">商品名</p>
            <p class="c66-sum-value c66-sum-name">{_sum_product_text}</p>
          </div>
          <div class="c66-sum-item">
            <p class="c66-sum-label">ブランド</p>
            <p class="c66-sum-value c66-sum-name">{_sum_brand_text}</p>
          </div>
          <div class="c66-sum-item">
            <p class="c66-sum-label">定価額</p>
            <p>{_sum_price_html}</p>
          </div>
          <div class="c66-sum-item">
            <p class="c66-sum-label">寸法</p>
            <p class="c66-sum-value c66-sum-dim">{_dim_text}</p>
          </div>
        </div>
        """
    )

    if candidates:
        brand = top.get("brand", "不明")
        confidence = float(top.get("confidence", 0))

        model_html = (
            f'<p class="c66-model">{top["model_name"]}</p>' if top.get("model_name") else ""
        )
        html(
            f"""
            <div class="c66-card">
              <p class="c66-eyebrow">Brand</p>
              <p class="c66-brandname">{brand}</p>
              {model_html}
            </div>
            """
        )

        st.progress(min(max(confidence, 0.0), 1.0), text=f"信頼度  {int(confidence * 100)}%")
        st.markdown(f"**判定根拠**　{top.get('evidence', '-')}")

        # 根拠URL
        ev_urls = top.get("evidence_urls") or []
        if ev_urls:
            st.markdown("**根拠ページ**")
            for u in ev_urls:
                st.markdown(f"- [{u}]({u})")

        # 定価
        list_price = top.get("list_price_jpy")
        if list_price:
            src = top.get("list_price_source_url")
            src_html = (
                f'<a href="{src}" target="_blank" style="font-size:.8rem;">出典ページを見る ↗</a>'
                if src else ""
            )
            html(
                f"""
                <div class="c66-card" style="display:flex;align-items:baseline;justify-content:space-between;gap:1rem;">
                  <div>
                    <p class="c66-price-label">推定定価</p>
                    <p class="c66-price">¥{list_price:,}</p>
                  </div>
                  <div style="text-align:right;">{src_html}</div>
                </div>
                """
            )
        else:
            st.info("定価は写真・検索結果からは特定できませんでした。")

        if len(candidates) > 1:
            with st.expander("他のブランド候補"):
                for c in candidates[1:]:
                    cc = int(float(c.get("confidence", 0)) * 100)
                    st.markdown(f"- **{c.get('brand')}** （信頼度 {cc}%） — {c.get('evidence', '')}")

    # 相場
    val = verdict.get("valuation", {})
    klo, khi = val.get("kaitori_jpy_low"), val.get("kaitori_jpy_high")
    mlo, mhi = val.get("mercari_jpy_low"), val.get("mercari_jpy_high")
    kaitori = f"¥{klo:,} 〜 ¥{khi:,}" if klo and khi else "要相見積もり"
    mercari = f"¥{mlo:,} 〜 ¥{mhi:,}" if mlo and mhi else "要調査"
    html(
        f"""
        <p class="c66-eyebrow" style="margin-top:1.6rem;">Market Estimate</p>
        <div class="c66-row">
          <div class="c66-card" style="flex:1;text-align:center;">
            <p class="c66-price-label">業者買取相場</p>
            <p style="font-family:'Cormorant Garamond',serif;font-size:1.5rem;font-weight:600;margin:.2rem 0 0;">{kaitori}</p>
          </div>
          <div class="c66-card" style="flex:1;text-align:center;">
            <p class="c66-price-label">メルカリ販売相場</p>
            <p style="font-family:'Cormorant Garamond',serif;font-size:1.5rem;font-weight:600;margin:.2rem 0 0;">{mercari}</p>
          </div>
        </div>
        """
    )

    rec = val.get("recommended_channel")
    if rec:
        html(
            f"""
            <div class="c66-card" style="background:#1F1D1A;border-color:#1F1D1A;">
              <p class="c66-price-label" style="color:#B8A98C;">Recommendation</p>
              <p style="color:#FBFAF7;font-size:1.15rem;font-weight:500;margin:.2rem 0 .3rem;">おすすめチャネル：{rec}</p>
              <p style="color:#D9D5CC;font-size:.9rem;margin:0;">{val.get('reasoning', '')}</p>
            </div>
            """
        )

    # ─────────────────────────────────────────────────────────
    # 利益シミュレーション
    #   casa66は家具を「無料引き取り」。小型は着払い発送（送料=購入者負担）／
    #   大型は引き取り限定（送料0）が基本 → 出品者の送料負担は原則0。
    #
    #   手数料率（目安）:
    #     - メルカリ 10%   … 出典: research-summary.md 手数料表（2026-05-25調査）
    #     - ジモティー 0%  … 出典: research-summary.md 手数料表（無料）
    #     - ヤフオク 10%   … ⚠️ 一般的な個人出品の目安（本アプリの写真研究データ外・要検証）
    #   ※いずれも「目安」。実際の料率は各サービスの最新規約を確認すること。
    # ─────────────────────────────────────────────────────────
    st.markdown('<p class="c66-eyebrow" style="margin-top:2.2rem;">Profit Simulation</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="c66-hint">想定の手取りをチャネル別に試算します。'
        'casa66は無料引き取り・送料は原則0（小型=着払い／大型=引き取り）が前提です。'
        '手数料率は目安なので、実際の売却前に各サービスの最新規約をご確認ください。</p>',
        unsafe_allow_html=True,
    )

    # 手数料率（目安）— 上のコメントに出典を明記
    FEE_RATES = {
        "メルカリ": ("0.10", 0.10, "research-summary.md（手数料表）"),
        "ジモティー": ("0%", 0.00, "research-summary.md（手数料表・無料）"),
        "ヤフオク": ("0.10", 0.10, "一般的な個人出品の目安 ※研究データ外・要検証"),
    }

    # 想定売却価格の初期値: メルカリ相場の中央値（low/high両方あれば）
    if isinstance(mlo, (int, float)) and isinstance(mhi, (int, float)) and mlo and mhi:
        _default_price = float((mlo + mhi) / 2)
    elif isinstance(mhi, (int, float)) and mhi:
        _default_price = float(mhi)
    elif isinstance(mlo, (int, float)) and mlo:
        _default_price = float(mlo)
    else:
        _default_price = 0.0

    sim_cols = st.columns(3)
    with sim_cols[0]:
        sale_price = st.number_input(
            "想定売却価格 (円)",
            min_value=0.0, step=500.0, value=_default_price,
            help="メルカリ相場の中央値を初期表示。実際に付ける値段に書き換えてOK。",
            key=f"sim_price_{run_id}",
        )
    with sim_cols[1]:
        seller_shipping = st.number_input(
            "出品者が負担する送料 (円)",
            min_value=0.0, step=100.0, value=0.0,
            help="小型=着払い／大型=引き取り が基本なので通常0。自分で送る場合だけ入力。",
            key=f"sim_ship_{run_id}",
        )
    with sim_cols[2]:
        packing_cost = st.number_input(
            "梱包・清掃コスト (円)",
            min_value=0.0, step=100.0, value=0.0,
            help="無料引き取りのため原価0が基本。資材費などが出た場合だけ入力。",
            key=f"sim_pack_{run_id}",
        )

    if not sale_price or sale_price <= 0:
        st.info(
            "💡 メルカリ相場が取れなかった場合は、想定売却価格を手入力してください。"
            "（手入力すると下の手取り試算が出ます）"
        )
    else:
        # 想定手取り = 売却価格 −(売却価格×手数料率)− 出品者送料 − 梱包清掃コスト
        rows = []
        best_channel = None
        best_net = None
        for ch, (rate_disp, rate, _src) in FEE_RATES.items():
            fee = sale_price * rate
            net = sale_price - fee - seller_shipping - packing_cost
            rows.append((ch, rate_disp, fee, net))
            if best_net is None or net > best_net:
                best_net = net
                best_channel = ch

        table_rows = ""
        for ch, rate_disp, fee, net in rows:
            is_best = ch == best_channel
            note = "（手数料の表示は目安）" if ch == "ヤフオク" else ""
            row_bg = "background:#F4F1EA;" if is_best else ""
            best_tag = ' <span style="color:var(--sand);font-weight:700;">★手取り最大</span>' if is_best else ""
            fee_disp = "—" if ch == "ジモティー" else f"¥{int(round(fee)):,}"
            table_rows += (
                f'<tr style="{row_bg}">'
                f'<td style="padding:.5rem .7rem;">{ch}{best_tag}</td>'
                f'<td style="padding:.5rem .7rem;text-align:center;">{rate_disp}</td>'
                f'<td style="padding:.5rem .7rem;text-align:right;">{fee_disp}</td>'
                f'<td style="padding:.5rem .7rem;text-align:right;font-weight:700;">¥{int(round(net)):,}</td>'
                f'</tr>'
            )

        html(
            f"""
            <div class="c66-card" style="padding:1.2rem 1.4rem;">
              <table style="width:100%;border-collapse:collapse;font-size:.92rem;">
                <thead>
                  <tr style="border-bottom:1px solid var(--line);color:var(--ink-soft);font-size:.78rem;letter-spacing:.06em;">
                    <th style="padding:.4rem .7rem;text-align:left;">チャネル</th>
                    <th style="padding:.4rem .7rem;text-align:center;">手数料率(目安)</th>
                    <th style="padding:.4rem .7rem;text-align:right;">手数料</th>
                    <th style="padding:.4rem .7rem;text-align:right;">想定手取り</th>
                  </tr>
                </thead>
                <tbody>{table_rows}</tbody>
              </table>
            </div>
            """
        )
        st.caption(
            "手数料率の目安の出典: メルカリ10%・ジモティー0% = research-summary.md（手数料表）／"
            "ヤフオク10% = 一般的な個人出品の目安（研究データ外・要検証）。"
        )
        st.markdown(
            "手数料0のジモティーが手取りは最大になりやすい傾向です。"
            "一方でメルカリは集客力が高い（買い手が多い）ため、早く売りたい時に向くことがあります。"
            "どちらが良いかは商品と急ぎ度合い次第で、断定はできません。"
        )

    html(
        f'<p style="color:var(--ink-soft);font-size:.9rem;margin-top:1.4rem;">'
        f'家具種別　{verdict.get("furniture_type", "-")}　／　素材　{verdict.get("material", "-")}</p>'
    )
    if verdict.get("condition_notes"):
        st.markdown(f"**状態メモ**　{verdict['condition_notes']}")

    if verdict.get("photo_quality_feedback"):
        st.info(f"📸 もっと精度を上げるには：{verdict['photo_quality_feedback']}")

    # ─────────────────────────────────────────────────────────
    # 寸法・重量（着払い判定＆出品文の生成に使う）
    # ─────────────────────────────────────────────────────────
    st.markdown('<p class="c66-eyebrow" style="margin-top:2.2rem;">Size &amp; Weight</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="c66-hint">着払い可否の判定と、下の出品文のサイズ欄に使います。'
        'AIの推定値が入っていますが、実測できる場合は上書きしてください。'
        '（0のままだと「未確定」として扱われます）</p>',
        unsafe_allow_html=True,
    )

    est_dims = verdict.get("estimated_dimensions_cm") or {}
    est_width = est_dims.get("width")
    est_depth = est_dims.get("depth")
    est_height = est_dims.get("height")
    est_weight = verdict.get("estimated_weight_kg")

    def _as_default(v):
        return float(v) if isinstance(v, (int, float)) else 0.0

    dim_cols = st.columns(4)
    with dim_cols[0]:
        width_in = st.number_input(
            "幅 (cm)", min_value=0.0, step=0.5, value=_as_default(est_width), key=f"width_{run_id}"
        )
    with dim_cols[1]:
        depth_in = st.number_input(
            "奥行 (cm)", min_value=0.0, step=0.5, value=_as_default(est_depth), key=f"depth_{run_id}"
        )
    with dim_cols[2]:
        height_in = st.number_input(
            "高さ (cm)", min_value=0.0, step=0.5, value=_as_default(est_height), key=f"height_{run_id}"
        )
    with dim_cols[3]:
        weight_in = st.number_input(
            "重量 (kg)", min_value=0.0, step=0.5, value=_as_default(est_weight), key=f"weight_{run_id}"
        )

    if not any(isinstance(v, (int, float)) for v in (est_width, est_depth, est_height, est_weight)):
        st.caption("※ 写真からは推定できませんでした。分かる範囲で入力してください。")

    def _none_if_zero(v: float):
        return v if v and v > 0 else None

    width_val = _none_if_zero(width_in)
    depth_val = _none_if_zero(depth_in)
    height_val = _none_if_zero(height_in)
    weight_val = _none_if_zero(weight_in)

    shipping = classify_shipping(width_val, depth_val, height_val, weight_val)

    if shipping["size_class"] == "unknown":
        st.warning(
            "⚠️ 寸法・重量が未確定のため、安全側の「大型版（引き取り限定）」を仮表示しています。"
            "実寸を入力すると、小型版（着払い対応）に切り替わる場合があります。"
        )
        effective_size_class = "large"
    elif shipping["needs_measurement_check"]:
        st.warning(f"⚠️ {shipping['reason']}")
        effective_size_class = shipping["size_class"]
    else:
        st.success(f"✅ {shipping['reason']}")
        effective_size_class = shipping["size_class"]

    # ─────────────────────────────────────────────────────────
    # 出品説明文（安心・誠実型テンプレ／アプリ別タブ）
    # ─────────────────────────────────────────────────────────
    st.markdown('<p class="c66-eyebrow" style="margin-top:2.2rem;">Listing Copy</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="c66-hint">投稿前に、文中の【】の箇所（傷の写真番号など）を実物に合わせて差し替えてください。</p>',
        unsafe_allow_html=True,
    )

    # 商品名（product_name）を優先して「商品名｜ブランド」ラベルを組み立てる
    item_name = compose_item_label(
        top.get("product_name"), top.get("brand"), top.get("model_name")
    )

    listings = build_all_listings(
        effective_size_class,
        item_name,
        verdict.get("material"),
        width_val,
        depth_val,
        height_val,
    )

    channel_items = list(LISTING_CHANNELS.items())
    listing_tabs = st.tabs([cfg["label"] for _, cfg in channel_items])
    for tab, (channel_key, _cfg) in zip(listing_tabs, channel_items):
        with tab:
            st.code(listings[channel_key], language=None)

    # ─────────────────────────────────────────────────────────
    # CSV出力（管理シート貼り付け用）
    # ─────────────────────────────────────────────────────────
    st.markdown('<p class="c66-eyebrow" style="margin-top:2.2rem;">Export</p>', unsafe_allow_html=True)

    def _csv_num(v):
        return v if v is not None else ""

    def _cod_label(shipping_result: dict) -> str:
        if shipping_result.get("cod_allowed") is True:
            return "可（小型）"
        if shipping_result.get("cod_allowed") is False:
            return "不可（大型・引き取り限定）"
        return "未確定"

    csv_buf = StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(
        [
            "査定日時", "商品名", "ブランド", "型番", "推定定価(円)",
            "幅(cm)", "奥行(cm)", "高さ(cm)", "重量(kg)",
            "業者買取下限(円)", "業者買取上限(円)",
            "メルカリ下限(円)", "メルカリ上限(円)",
            "着払い可否", "おすすめチャネル",
        ]
    )
    writer.writerow(
        [
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            top.get("product_name") or "",
            top.get("brand", ""),
            top.get("model_name") or "",
            _csv_num(top.get("list_price_jpy")),
            _csv_num(width_val),
            _csv_num(depth_val),
            _csv_num(height_val),
            _csv_num(weight_val),
            _csv_num(val.get("kaitori_jpy_low")),
            _csv_num(val.get("kaitori_jpy_high")),
            _csv_num(val.get("mercari_jpy_low")),
            _csv_num(val.get("mercari_jpy_high")),
            _cod_label(shipping),
            val.get("recommended_channel", ""),
        ]
    )
    csv_bytes = csv_buf.getvalue().encode("utf-8-sig")  # Excelで文字化けしないようBOM付き

    st.download_button(
        "📄 査定結果をCSVでダウンロード（管理シート貼り付け用）",
        data=csv_bytes,
        file_name=f"casa66_kaitori_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        key=f"csv_dl_{run_id}",
    )

    # デバッグ
    with st.expander("🔍 Vision検出の生データ（デバッグ用）"):
        if result_engine == "claude":
            st.write("Claudeエンジンで実行されました（Web検索で裏取り。Cloud Visionは未使用）。")
        elif result_skip_vision:
            st.write("Vision無効モードで実行されました。")
        else:
            st.markdown("**Best Guess Labels:**")
            st.write(vision_ctx.best_guess_labels)
            st.markdown("**Web Entities:**")
            st.write(vision_ctx.web_entities)
            st.markdown("**類似画像があるページ:**")
            for p in vision_ctx.pages:
                st.markdown(f"- [{p.domain_class}] [{p.title or p.url}]({p.url})")

    with st.expander("生のAI応答（デバッグ用）"):
        st.json(verdict)
