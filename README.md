# 全銀フォーマット変換ツール

請求書PDFを Claude API で解析し、銀行への振込データ（全銀フォーマット）を生成する Streamlit Web アプリ。

## 主な機能

- 請求書PDFのアップロード（複数同時可、後から追加も可）
- Claude API による振込先情報の自動抽出（受取人名・銀行・支店・口座番号・金額）
- 抽出結果のプレビュー + 編集（PDFサムネイル横にフォーム）
- 振込情報の一覧表示・編集
- 取引先マスタ（MoneyForward CSV 取り込み対応）
- マスタとの自動マッチング・手動上書き（プルダウン選択）
- 振込日ごとの「振込バッチ」自動保存・復元
- 全銀フォーマット (.txt) 生成 → ワンクリック自動ダウンロード

## セットアップ（ローカル）

```powershell
git clone <repo-url>
cd zengin

# 仮想環境
py -m venv .venv
.\.venv\Scripts\Activate.ps1

# 依存パッケージ
pip install -e . streamlit

# 設定ファイルを雛形からコピーして編集
copy config.example.yaml config.yaml
# config.yaml に銀行口座情報・API キーを入力
```

## 起動

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run webapp/app.py --server.port=8508 --server.address=127.0.0.1
```

または `run_webapp.bat` をダブルクリック。ブラウザで `http://localhost:8508` を開く。

## API キーの設定

優先順位は以下:

1. `config.yaml` の `claude.api_key`（ローカル開発用、リポジトリにはコミットされない）
2. 環境変数 `ANTHROPIC_API_KEY`
3. Streamlit Secrets の `ANTHROPIC_API_KEY`（クラウドデプロイ時）

## Streamlit Community Cloud へのデプロイ

1. https://share.streamlit.io にアクセスし、GitHub 連携でログイン
2. 「New app」→ このリポジトリを選択
3. メインファイルパスに `webapp/app.py` を指定
4. 「Advanced settings」→ Secrets に以下を貼り付け:

   ```toml
   ANTHROPIC_API_KEY = "sk-ant-api03-..."
   ```

5. デプロイ実行

**注意**: Streamlit Community Cloud の無料プランではコンテナのストレージが永続化されません。`payees.db` `batches.db` はコンテナ再起動でリセットされるため、マスタは毎回 CSV から再取り込みする必要があります。

## ファイル構成

```
zengin/
├─ src/zengin_converter/       # コアロジック（PDF解析、抽出、全銀ビルダー）
├─ webapp/                     # Streamlit Web UI
│  ├─ app.py                   # メインアプリ
│  ├─ payee_db.py              # 取引先マスタの SQLite ラッパー
│  └─ batch_db.py              # 振込バッチの SQLite ラッパー
├─ config.example.yaml         # 設定ファイル雛形（実値は config.yaml に）
├─ run_webapp.bat              # Windows 用起動スクリプト
└─ pyproject.toml
```

## ライセンス

社内利用限定。
