@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   casa66 中古家具 AI査定アプリ 起動ツール
echo ============================================
echo.

REM --- 1. Python が入っているか確認 ---
where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python が見つかりませんでした。
    echo     このパソコンにはまだセットアップが必要です。
    echo     お手数ですが たいきさん に連絡してください。
    echo.
    pause
    exit /b 1
)

REM --- 2. Streamlit が入っているか確認。無ければ初回だけ依存をインストール ---
python -c "import streamlit" >nul 2>nul
if errorlevel 1 (
    echo [初回セットアップ] 必要な部品を準備しています。少し時間がかかります...
    echo.
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [!] 準備中にエラーが発生しました。
        echo     お手数ですが たいきさん に連絡してください。
        echo.
        pause
        exit /b 1
    )
    echo.
    echo [初回セットアップ完了]
    echo.
)

REM --- 3. APIキーを api_key.txt から読み込む（ファイルにベタ書きしない） ---
if not exist "api_key.txt" (
    echo [!] api_key.txt が見つかりません。
    echo     たいきさんからキーを受け取って、このフォルダに
    echo     「api_key.txt」という名前で保存してください。
    echo     （中身はキーを1行だけ貼り付けたテキストファイルです）
    echo.
    pause
    exit /b 1
)

REM api_key.txt の1行目を読み取って環境変数にセット
set "ANTHROPIC_API_KEY="
for /f "usebackq delims=" %%K in ("api_key.txt") do (
    if not defined ANTHROPIC_API_KEY set "ANTHROPIC_API_KEY=%%K"
)

if not defined ANTHROPIC_API_KEY (
    echo [!] api_key.txt の中身が空のようです。
    echo     たいきさんから受け取ったキーを貼り付けて保存し直してください。
    echo.
    pause
    exit /b 1
)

REM --- 4. アプリを起動（ブラウザが自動で開きます） ---
echo アプリを起動します。ブラウザが自動で開きます...
echo （終了するときは、この黒い画面で Ctrl + C を押すか、閉じてください）
echo.
python -m streamlit run app.py

REM --- 5. エラーで落ちても画面が即閉じしないよう最後に一時停止 ---
echo.
echo アプリを終了しました。
pause
endlocal
