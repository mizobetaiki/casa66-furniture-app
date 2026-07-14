#!/bin/bash
# casa66 中古家具 AI査定アプリ 起動ツール（Mac用）
# ダブルクリックで起動できます。

# このスクリプトのあるフォルダへ移動
cd "$(dirname "$0")" || exit 1

echo "============================================"
echo "  casa66 中古家具 AI査定アプリ 起動ツール"
echo "============================================"
echo ""

# --- 1. Python が入っているか確認 ---
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "[!] Python が見つかりませんでした。"
    echo "    このパソコンにはまだセットアップが必要です。"
    echo "    お手数ですが たいきさん に連絡してください。"
    echo ""
    read -n 1 -s -r -p "何かキーを押すと閉じます..."
    exit 1
fi

# --- 2. Streamlit が入っているか確認。無ければ初回だけ依存をインストール ---
if ! "$PY" -c "import streamlit" >/dev/null 2>&1; then
    echo "[初回セットアップ] 必要な部品を準備しています。少し時間がかかります..."
    echo ""
    "$PY" -m pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo ""
        echo "[!] 準備中にエラーが発生しました。"
        echo "    お手数ですが たいきさん に連絡してください。"
        echo ""
        read -n 1 -s -r -p "何かキーを押すと閉じます..."
        exit 1
    fi
    echo ""
    echo "[初回セットアップ完了]"
    echo ""
fi

# --- 3. APIキーを api_key.txt から読み込む（ファイルにベタ書きしない） ---
if [ ! -f "api_key.txt" ]; then
    echo "[!] api_key.txt が見つかりません。"
    echo "    たいきさんからキーを受け取って、このフォルダに"
    echo "    「api_key.txt」という名前で保存してください。"
    echo "    （中身はキーを1行だけ貼り付けたテキストファイルです）"
    echo ""
    read -n 1 -s -r -p "何かキーを押すと閉じます..."
    exit 1
fi

# api_key.txt の1行目を読み取って環境変数にセット（前後の空白・改行を除去）
ANTHROPIC_API_KEY="$(head -n 1 api_key.txt | tr -d '\r\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
export ANTHROPIC_API_KEY

if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "[!] api_key.txt の中身が空のようです。"
    echo "    たいきさんから受け取ったキーを貼り付けて保存し直してください。"
    echo ""
    read -n 1 -s -r -p "何かキーを押すと閉じます..."
    exit 1
fi

# --- 4. アプリを起動（ブラウザが自動で開きます） ---
echo "アプリを起動します。ブラウザが自動で開きます..."
echo "（終了するときは、この画面で Ctrl + C を押すか、閉じてください）"
echo ""
"$PY" -m streamlit run app.py

# --- 5. エラーで落ちても画面が即閉じしないよう最後に一時停止 ---
echo ""
echo "アプリを終了しました。"
read -n 1 -s -r -p "何かキーを押すと閉じます..."
