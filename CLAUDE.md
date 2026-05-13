# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 開発コマンド

Windows / PowerShell + `.venv` を前提。

```powershell
# venv 起動
.\.venv\Scripts\Activate.ps1

# テスト（pytest）
py -m pytest                                       # 全テスト
py -m pytest tests/test_zengin_builder.py          # 1ファイル
py -m pytest tests/test_zengin_builder.py::test_header_length  # 1ケース
py -m pytest -k "header"                           # キーワードフィルタ

# Streamlit Web アプリ起動（ローカル開発）
streamlit run webapp/app.py --server.port=8508 --server.address=127.0.0.1
# or
.\run_webapp.bat

# tkinter GUI 起動（旧版、保守用）
py run_gui.py

# CLI（請求書PDF → 全銀ファイル単発変換、Streamlit以外の経路）
zengin-converter sample/invoice.pdf --output output/transfers.txt

# 全銀フォーマットファイルの差分検証（既存ファイルと生成ファイルのバイト位置検査）
py webapp/compare_zengin.py <fileA> <fileB>
```

依存追加・変更時:
```powershell
pip install -e . streamlit          # ローカル開発（editable install）
# pyproject.toml と requirements.txt の両方を更新する必要がある
# pyproject.toml = ローカル開発の依存
# requirements.txt = Streamlit Community Cloud のビルド用
```

## アーキテクチャ

**請求書PDF → Claude API による情報抽出 → 全銀フォーマット(120バイト固定長)生成** という1本のパイプラインを、CLI / tkinter GUI / Streamlit Web の3経路から触れる構成。

### 2層構造

- `src/zengin_converter/` — コアロジック（純粋なPython、pip パッケージとして動作）
- `webapp/` — Streamlit Web UI 層（SQLite 永続化と UI ステート管理）

`webapp/app.py` の先頭で `sys.path.insert(0, ../src)` を実行している。これは Streamlit Community Cloud が `pip install -e .` を実行しないため、editable install なしでも `zengin_converter` モジュールを import できるようにするための工夫。**新しいコアロジックを `src/zengin_converter/` に追加した場合、`webapp/app.py` は何もしなくても import 可能**。

### コアパイプライン（src/zengin_converter/）

```
pdf_reader.read_pdf()           # PyMuPDFでテキスト抽出、不足ならOCRフォールバック
  ↓ PdfContent (text + base64)
extractor.extract_invoice()     # Claude API (tool_use) で構造化抽出、失敗時regex
  ↓ InvoiceData (pydantic)
zengin_writer.generate_zengin() # 同一振込先合算 → bank_resolver でコード解決
  ↓ resolved + merged transfers
zengin_builder.build_*()        # 120バイト固定長レコード組み立て (cp932)
  ↓ bytes
.txt ファイル出力
```

**重要な仕様**: 全銀フォーマットの各レコードは120バイト固定長で、半角カナフィールドに2バイト文字が1文字でも混入すると後続のレコードが全部ズレる。`zengin_builder` の最後に `assert len(record) == RECORD_LENGTH` がある。

### 半角カナ正規化（kana_utils.to_halfwidth_kana）

cp932 で2バイトになる文字を半角フィールドから排除するためのフィルタ。特に注意:
- 全角ハイフン系8種 (`ー`, `－`, `‐`, `‒`, `–`, `—`, `―`, `−`) → すべて半角 `-` に統一
- `zengin-code` パッケージの `bank.kana` には全角文字が混入することがあるため、`bank_resolver.get_bank_name_kana()` と `get_branch_name_kana()` でも必ず `to_halfwidth_kana` を通している
- 過去にこのフィルタを通さず直接 `jaconv.z2h` だけだったため、`住信ＳＢＩネット` が `ｽﾐｼﾝｴｽﾋﾞ－ｱｲﾈﾂﾄ` （全角ハイフン混入）になるバグがあった

### Claude API の抽出ロジック（extractor.py）

`build_system_prompt(payee_hints)` が動的に system prompt を組み立てる:
1. ベースプロンプトに「振込先（受取人）vs 請求書宛先（支払者）」「金額の選び方」「銀行/支店コードの括弧表記」「完全なレイアウト例とJSON正解」を明記
2. `payee_hints`（マスタの取引先名一覧）を末尾に追加 → 既知顧客のカナ表記が統一される

抽出失敗時は `extract_with_regex` にフォールバック。

### Webapp の状態管理（webapp/app.py）

3つの SQLite DB:
- `payees.db` (payee_db.py) — 取引先マスタ。MoneyForward CSV 取り込み対応、`find_match()` で口座番号→名義人カナ→取引先名の優先順マッチング
- `batches.db` (batch_db.py) — 振込バッチ。PDFバイナリをBLOBで保存、`batches.consignor_id` で振込元を紐付け
- `consignors.db` (consignor_db.py) — 振込元情報（複数登録可、`is_default` で1件マーク）

`st.session_state` のキー設計:
- `extracted_items` — 抽出済みアイテムのリスト `[{id, filename, pdf_bytes, invoice, page_count, matched_payee_key}]`
- 各 widget は `f"item_{id}_payee"` `f"item_{id}_bank_sel"` 等のキーで個別管理
- `current_batch_id` / `current_consignor_id` — 現在開いているバッチ/振込元

**重要な暗黙ルール**:
- `st.session_state.items` は dict の `items()` メソッドと衝突するので使用禁止 → `st.session_state["extracted_items"]` を使う
- バッチ復元時 (`load_batch_into_state`) は `clear_item_widget_keys()` で widget の session_state を全削除してから DB の invoice 値で再初期化する。これをしないと前のバッチの widget 値が残る
- フォーム手動編集（text_input 等）は scriptが走るたびに保存しない → 主要操作（マスタ選択、行削除、PDF追加、全銀生成）or 明示保存ボタンのタイミングで `save_current_batch_to_db()` を呼ぶ

### API キーの解決順序

`_resolve_api_key()` の優先順位:
1. `config.yaml` の `claude.api_key`（ローカル開発、`.gitignore`で除外）
2. 環境変数 `ANTHROPIC_API_KEY`
3. `st.secrets["ANTHROPIC_API_KEY"]`（Streamlit Cloud）

パスワードゲート（`require_password()`）も同じパターンで `APP_PASSWORD` を解決。未設定なら認証スキップ。

### Streamlit のモジュールリロード

**`webapp/app.py` の変更は自動検知してリロードされるが、`src/zengin_converter/` 配下や `webapp/*_db.py` などの依存モジュールはモジュールキャッシュが効くため自動リロードされない**。コアロジックを変更した場合は Streamlit サーバーを止めて起動し直す必要がある。

## デプロイと運用上の制約

- ホスティング: Streamlit Community Cloud（Public app + パスワードゲートで保護）
- データ永続化なし: コンテナ再起動で `payees.db` `batches.db` `consignors.db` がリセット → 起動時に `consignor_db.seed_from_yaml_config()` で `config.yaml` から1件シードするが、マスタ・バッチは毎回CSVから再投入が必要
- 1ワークスペース1 private app の制限のため、リポジトリは Public 化

## .gitignore のポリシー

機密ファイル（`config.yaml`、`*.db`、`*.sqlite`、`PDF/`、`sample/`、`output/`）は確実に除外。`config.example.yaml` のみコミット対象。

## tkinter GUI（src/zengin_converter/gui.py）

旧版の単体アプリ。Streamlit 版が機能的に上位互換だが、削除はしていない。新機能は **Streamlit 版にのみ実装する**方針。tkinter版は exe ビルド済み（`dist/全銀変換ツール.exe`、`zengin_converter.spec`）で、過去にPyInstallerでビルドされたものが残っている。
