# 全銀フォーマット変換ツール Web UI

請求書PDFをアップロード → Claude APIで抽出 → 全銀フォーマット (.txt) をダウンロード。

## 初回セットアップ

```powershell
cd C:\Users\river\projects\zengin
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e . streamlit
```

## 起動

プロジェクトルートの `run_webapp.bat` をダブルクリック、または:

```powershell
cd C:\Users\river\projects\zengin
.\.venv\Scripts\Activate.ps1
streamlit run webapp\app.py --server.port=8508 --server.address=127.0.0.1
```

ブラウザで `http://localhost:8508` を開いてください。

> ポート 8508 を使うのは、`mf-journal-db` の Streamlit（MF仕訳パターン検索）がデフォルトの 8501 を使うため、競合を避けるためです。

## 使い方

1. 「振込元設定」タブで委託者情報・振込元口座を確認/保存
2. 「変換」タブで請求書PDFをアップロード
3. 「PDF読込・抽出」をクリック → Claude が解析
4. 抽出結果テーブルで内容を確認・編集（セルクリックで編集可、行追加・削除も可）
5. 「全銀ファイル生成」 → 「ダウンロード」ボタンで .txt 取得

## 注意

- APIキーは `config.yaml` に平文保存されます。共有・公開は不可
- 同一振込先（銀行+支店+口座番号+種目）の請求書は自動的に金額が合算されます
