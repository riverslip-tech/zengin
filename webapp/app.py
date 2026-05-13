"""全銀フォーマット変換ツール Streamlit Web UI

起動: streamlit run webapp/app.py
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

# Streamlit Cloud など `pip install -e .` を実行しない環境でも
# zengin_converter をインポートできるように src/ を sys.path に追加する
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st
import streamlit.components.v1 as components
import yaml
from PIL import Image

from zengin_converter.extractor import extract_invoice
from zengin_converter.kana_utils import to_halfwidth_kana
from zengin_converter.models import ConsignorConfig, InvoiceData
from zengin_converter.pdf_reader import read_pdf
from zengin_converter.zengin_writer import generate_zengin

import batch_db
import payee_db


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_MODEL = "claude-sonnet-4-6"
ACCOUNT_TYPES = ["普通", "当座", "貯蓄", "その他"]


@st.cache_resource(show_spinner=False)
def get_db():
    return payee_db.get_conn()


@st.cache_resource(show_spinner=False)
def get_batch_db():
    return batch_db.get_conn()


def invoice_from_payee_row(row, fallback_amount: int = 0) -> InvoiceData:
    """マスタ1行を InvoiceData に変換する。金額は fallback_amount を入れる。"""
    payee_name = row["holder_kana"] or row["holder_name"] or row["payee_name"] or ""
    return InvoiceData(
        payee_name=payee_name,
        bank_name=row["bank_name"] or None,
        bank_code=row["bank_code"] or None,
        branch_name=row["branch_name"] or None,
        branch_code=row["branch_code"] or None,
        account_type=row["account_type"] or "普通",
        account_number=row["account_number"] or "",
        amount=fallback_amount,
    )


def apply_invoice_to_widgets(item_id: str, inv: InvoiceData) -> None:
    """invoice の値で widget の session_state を書き換える（次回 render に反映）。"""
    st.session_state[f"item_{item_id}_payee"] = to_halfwidth_kana(inv.payee_name)

    banks = get_bank_options()
    bank_idx = 0
    if inv.bank_code:
        for i, (c, _) in enumerate(banks):
            if c == inv.bank_code:
                bank_idx = i + 1
                break
    st.session_state[f"item_{item_id}_bank_sel"] = bank_idx

    branch_idx = 0
    if inv.bank_code and inv.branch_code:
        branches = get_branch_options(inv.bank_code)
        for i, (c, _) in enumerate(branches):
            if c == inv.branch_code:
                branch_idx = i + 1
                break
    st.session_state[f"item_{item_id}_branch_sel"] = branch_idx

    st.session_state[f"item_{item_id}_acc_type"] = inv.account_type or "普通"
    st.session_state[f"item_{item_id}_acc_num"] = inv.account_number or ""
    st.session_state[f"item_{item_id}_amount"] = int(inv.amount or 0)


def on_master_change(item_id: str) -> None:
    """マスタ selectbox が変更されたときのコールバック。"""
    key = f"item_{item_id}_master_sel"
    sel_raw = st.session_state.get(key, 0)
    try:
        sel = int(sel_raw)
    except (TypeError, ValueError):
        # Streamlit の rerun 過程で稀に str が入るケースの保険
        print(f"[on_master_change] non-int master_sel value: {sel_raw!r} (type={type(sel_raw).__name__})")
        sel = 0

    conn = get_db()
    all_rows = payee_db.list_all(conn)

    items = st.session_state.get("extracted_items", [])
    for it in items:
        if it["id"] != item_id:
            continue
        amount = it["invoice"].amount if it.get("invoice") else 0
        if sel <= 0 or sel - 1 >= len(all_rows):
            it["matched_payee_key"] = None
        else:
            row = all_rows[sel - 1]
            new_invoice = invoice_from_payee_row(row, fallback_amount=amount)
            it["invoice"] = new_invoice
            it["matched_payee_key"] = row["account_unique_key"]
            apply_invoice_to_widgets(item_id, new_invoice)
        break
    st.session_state.zengin_bytes = None
    # マスタ変更後の状態を DB に保存
    save_current_batch_to_db(from_widgets=False)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _resolve_api_key(claude_cfg: dict) -> str:
    """API キーを config.yaml → 環境変数 → Streamlit Secrets の順で解決する。

    Streamlit Cloud にデプロイする場合は Secrets に
    `ANTHROPIC_API_KEY = "sk-ant-..."` を登録すれば自動的に読まれる。
    """
    # 1. config.yaml の値（ローカル開発時のみ想定、リポジトリにはコミットされない）
    val = (claude_cfg.get("api_key") or "").strip()
    if val:
        return val
    # 2. 環境変数
    val = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if val:
        return val
    # 3. Streamlit Secrets
    try:
        val = (st.secrets.get("ANTHROPIC_API_KEY") or "").strip()
        if val:
            return val
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError, Exception):
        pass
    return ""


def save_config(data: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def init_state() -> None:
    if "config_data" not in st.session_state:
        st.session_state.config_data = load_config()
    if "extracted_items" not in st.session_state:
        st.session_state["extracted_items"] = []
    if "zengin_bytes" not in st.session_state:
        st.session_state.zengin_bytes = None
    if "zengin_filename" not in st.session_state:
        st.session_state.zengin_filename = "transfers.txt"
    if "current_batch_id" not in st.session_state:
        # 起動時、直近のバッチがあれば自動ロード
        bconn = get_batch_db()
        latest = batch_db.latest_batch_id(bconn)
        st.session_state.current_batch_id = latest
        if latest:
            load_batch_into_state(latest)


def clear_item_widget_keys() -> None:
    """全 item に紐付く widget の session_state を削除する。"""
    for k in list(st.session_state.keys()):
        if k.startswith("item_"):
            del st.session_state[k]


def load_batch_into_state(batch_id: str) -> None:
    """DBからバッチを読み出して session_state['extracted_items'] に展開する。"""
    bconn = get_batch_db()
    rows = batch_db.list_items(bconn, batch_id)
    new_items: list[dict[str, Any]] = []
    for r in rows:
        inv = InvoiceData(
            payee_name=r["payee_name"] or "",
            bank_name=r["bank_name"] or None,
            bank_code=r["bank_code"] or None,
            branch_name=r["branch_name"] or None,
            branch_code=r["branch_code"] or None,
            account_type=r["account_type"] or "普通",
            account_number=r["account_number"] or "",
            amount=int(r["amount"] or 0),
        )
        new_items.append({
            "id": r["item_id"],
            "filename": r["filename"] or "",
            "pdf_bytes": bytes(r["pdf_blob"]) if r["pdf_blob"] else b"",
            "invoice": inv,
            "page_count": int(r["page_count"] or 1),
            "matched_payee_key": r["matched_payee_key"],
        })
    clear_item_widget_keys()
    st.session_state["extracted_items"] = new_items
    st.session_state.current_batch_id = batch_id
    st.session_state.zengin_bytes = None


def get_current_invoice_for_item(item: dict) -> InvoiceData:
    """widget の最新値を取得、widget未renderなら item['invoice'] を返す。"""
    item_id = item["id"]
    if f"item_{item_id}_payee" in st.session_state:
        return build_invoice_from_widgets(item_id)
    return item["invoice"]


def save_current_batch_to_db(from_widgets: bool = False) -> None:
    """現在の session_state["extracted_items"] を DB に書き戻す。

    from_widgets=True なら widget の最新値を使う（ボタン押下時など）。
    """
    batch_id = st.session_state.get("current_batch_id")
    if not batch_id:
        return
    bconn = get_batch_db()
    items_to_save: list[dict] = []
    for i, it in enumerate(st.session_state.get("extracted_items", [])):
        if from_widgets:
            inv = get_current_invoice_for_item(it)
        else:
            inv = it["invoice"]
        items_to_save.append({
            "item_id": it["id"],
            "order_index": i,
            "filename": it["filename"],
            "pdf_blob": it.get("pdf_bytes") or b"",
            "page_count": it.get("page_count", 1),
            "payee_name": inv.payee_name or "",
            "bank_name": inv.bank_name or "",
            "bank_code": inv.bank_code or "",
            "branch_name": inv.branch_name or "",
            "branch_code": inv.branch_code or "",
            "account_type": inv.account_type or "普通",
            "account_number": inv.account_number or "",
            "amount": int(inv.amount or 0),
            "matched_payee_key": it.get("matched_payee_key"),
        })
    batch_db.replace_items(bconn, batch_id, items_to_save)


@st.cache_data(show_spinner=False, max_entries=64)
def render_pdf_page(pdf_bytes: bytes, page_index: int = 0, dpi: int = 130) -> Image.Image:
    """PDFの指定ページを PIL Image として返す（キャッシュ付き）。"""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_index = max(0, min(page_index, len(doc) - 1))
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()
    return img


@st.cache_data(show_spinner=False)
def get_bank_options() -> list[tuple[str, str]]:
    """全銀行 [(code, name), ...] をリストで返す。"""
    from zengin_code import Bank

    return [(code, bank.name) for code, bank in Bank.all.items()]


@st.cache_data(show_spinner=False)
def get_branch_options(bank_code: str) -> list[tuple[str, str]]:
    """指定銀行の全支店 [(code, name), ...] をリストで返す。"""
    from zengin_code import Bank

    bank = Bank.all.get(bank_code)
    if not bank:
        return []
    return [(code, branch.name) for code, branch in bank.branches.items()]


def _normalize_for_match(text: str) -> str:
    """全角英数→半角、空白除去、接尾辞「銀行/支店/信用金庫」を落とす。"""
    import jaconv

    s = jaconv.z2h(text, kana=False, ascii=True, digit=True).strip()
    for suffix in ("銀行", "信用金庫", "信金", "信用組合", "労働金庫", "支店", "出張所", "営業部"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.replace(" ", "").replace("　", "")


def resolve_bank_index(bank_code: str | None, bank_name: str | None) -> int | None:
    """銀行リストの中で、抽出結果に最も近い項目のインデックスを返す。見つからなければ None。"""
    banks = get_bank_options()
    if bank_code:
        for i, (c, _) in enumerate(banks):
            if c == bank_code:
                return i
    if bank_name:
        nm = _normalize_for_match(bank_name)
        if not nm:
            return None
        # 完全一致 → 前方一致 → 部分一致 の順
        normalized = [(i, _normalize_for_match(n)) for i, (_, n) in enumerate(banks)]
        for i, n in normalized:
            if n == nm:
                return i
        for i, n in normalized:
            if n and (n.startswith(nm) or nm.startswith(n)):
                return i
        for i, n in normalized:
            if n and (nm in n or n in nm):
                return i
    return None


def resolve_branch_index(bank_code: str, branch_code: str | None, branch_name: str | None) -> int | None:
    branches = get_branch_options(bank_code)
    if branch_code:
        for i, (c, _) in enumerate(branches):
            if c == branch_code:
                return i
    if branch_name:
        nm = _normalize_for_match(branch_name)
        if not nm:
            return None
        normalized = [(i, _normalize_for_match(n)) for i, (_, n) in enumerate(branches)]
        for i, n in normalized:
            if n == nm:
                return i
        for i, n in normalized:
            if n and (n.startswith(nm) or nm.startswith(n)):
                return i
        for i, n in normalized:
            if n and (nm in n or n in nm):
                return i
    return None


def build_invoice_from_widgets(item_id: str) -> InvoiceData:
    """セッションのウィジェット値から InvoiceData を組み立てる。"""
    g = lambda field: st.session_state.get(f"item_{item_id}_{field}", "")

    # 銀行: selectbox の選択インデックスから code/name を取得
    bank_sel = g("bank_sel")  # int or None
    bank_code: str | None = None
    bank_name: str | None = None
    if isinstance(bank_sel, int) and bank_sel > 0:
        banks = get_bank_options()
        bank_code, bank_name = banks[bank_sel - 1]

    branch_sel = g("branch_sel")
    branch_code: str | None = None
    branch_name: str | None = None
    if bank_code and isinstance(branch_sel, int) and branch_sel > 0:
        branches = get_branch_options(bank_code)
        if branch_sel - 1 < len(branches):
            branch_code, branch_name = branches[branch_sel - 1]

    return InvoiceData(
        payee_name=str(g("payee")).strip(),
        bank_name=bank_name,
        bank_code=bank_code,
        branch_name=branch_name,
        branch_code=branch_code,
        account_type=str(g("acc_type")).strip() or "普通",
        account_number=str(g("acc_num")).strip(),
        amount=int(g("amount") or 0),
    )


def _items_to_summary_df(items: list[dict]):
    """items を一覧表用 DataFrame に変換。widget があれば widget 値、なければ invoice。"""
    import pandas as pd

    rows = []
    for i, it in enumerate(items):
        inv = get_current_invoice_for_item(it)
        rows.append({
            "_id": it["id"],
            "#": i + 1,
            "ファイル": it["filename"],
            "受取人名": to_halfwidth_kana(inv.payee_name) if inv.payee_name else "",
            "銀行": f"{inv.bank_code or '----'}  {inv.bank_name or ''}".strip(),
            "支店": f"{inv.branch_code or '---'}  {inv.branch_name or ''}".strip(),
            "種目": inv.account_type or "普通",
            "口座番号": inv.account_number or "",
            "金額": int(inv.amount or 0),
            "マスタ": "✦" if it.get("matched_payee_key") else "",
        })
    return pd.DataFrame(rows)


def render_summary_table() -> None:
    """振込情報のサマリー一覧（編集可: 受取人名・種目・口座番号・金額）。

    銀行・支店はプルダウンが必要なので、ここでは表示のみ。
    銀行・支店を変えたい場合は下の個別フォームで編集する。
    """
    with st.expander("📋 振込情報一覧（受取人名・種目・口座番号・金額をここから直接編集できます）", expanded=True):
        df = _items_to_summary_df(st.session_state["extracted_items"])
        edited = st.data_editor(
            df,
            key="summary_editor",
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            disabled=["_id", "#", "ファイル", "銀行", "支店", "マスタ"],
            column_config={
                "_id": None,  # 非表示
                "#": st.column_config.NumberColumn(width="small"),
                "ファイル": st.column_config.TextColumn(width="medium"),
                "受取人名": st.column_config.TextColumn(width="medium"),
                "銀行": st.column_config.TextColumn(width="medium", help="変更は下の個別フォームから"),
                "支店": st.column_config.TextColumn(width="medium", help="変更は下の個別フォームから"),
                "種目": st.column_config.SelectboxColumn(
                    options=ACCOUNT_TYPES, required=True, width="small",
                ),
                "口座番号": st.column_config.TextColumn(width="small"),
                "金額": st.column_config.NumberColumn(format="%d", width="small"),
                "マスタ": st.column_config.TextColumn(width="small"),
            },
        )

        # 合計表示
        total = int(edited["金額"].sum())
        st.markdown(f"**合計: {total:,} 円　／　{len(edited)} 件**")

        if st.button("一覧の編集を反映", type="primary", key="summary_apply"):
            apply_summary_edits(edited)
            st.success("反映しました")
            st.rerun()


def apply_summary_edits(edited_df) -> None:
    """data_editor の変更内容を invoice / widget / DB に反映する。"""
    items = st.session_state["extracted_items"]
    id_to_item = {it["id"]: it for it in items}

    for _, row in edited_df.iterrows():
        item_id = row["_id"]
        it = id_to_item.get(item_id)
        if not it:
            continue
        # 既存の invoice をベースに、編集可能な4項目だけ上書き
        base = get_current_invoice_for_item(it)
        new_inv = InvoiceData(
            payee_name=str(row["受取人名"]).strip(),
            bank_name=base.bank_name,
            bank_code=base.bank_code,
            branch_name=base.branch_name,
            branch_code=base.branch_code,
            account_type=str(row["種目"]).strip() or "普通",
            account_number=str(row["口座番号"]).strip(),
            amount=int(row["金額"] or 0),
        )
        it["invoice"] = new_inv
        # widget も同期
        apply_invoice_to_widgets(item_id, new_inv)

    save_current_batch_to_db(from_widgets=False)
    st.session_state.zengin_bytes = None


def _trigger_browser_download(file_bytes: bytes, filename: str) -> None:
    """ブラウザ側で <a download> を自動クリックしてダウンロードを開始する。"""
    b64 = base64.b64encode(file_bytes).decode("ascii")
    safe_name = filename.replace('"', "")
    html = f"""
    <html><body>
    <a id="auto_dl_anchor"
       href="data:application/octet-stream;base64,{b64}"
       download="{safe_name}"></a>
    <script>
      setTimeout(() => {{
        const a = document.getElementById('auto_dl_anchor');
        if (a) a.click();
      }}, 100);
    </script>
    </body></html>
    """
    components.html(html, height=0)


def render_quick_generate(
    cfg: dict,
    transfer_type: str,
    transfer_date: str,
    output_filename: str,
) -> None:
    """一覧の直下に置く「生成→自動ダウンロード」のワンクリックボタン。"""
    with st.container(border=True):
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown("#### ⚡ 全銀ファイルを生成してダウンロード")
            st.caption("一覧の内容で全銀フォーマットファイルを生成し、ブラウザで自動ダウンロードします。")
        with c2:
            if st.button(
                "生成 → ダウンロード",
                type="primary",
                use_container_width=True,
                key="quick_gen_dl",
            ):
                # 直前にフォームの最新値を取り込んでから生成
                run_generation(cfg, transfer_type, transfer_date, output_filename)
                if st.session_state.zengin_bytes is not None:
                    st.session_state["auto_download_pending"] = True
                    st.rerun()

        # 自動ダウンロード実行（クリック直後の rerun で1回だけ走る）
        if st.session_state.get("auto_download_pending") and st.session_state.zengin_bytes:
            _trigger_browser_download(
                st.session_state.zengin_bytes,
                st.session_state.zengin_filename,
            )
            st.session_state["auto_download_pending"] = False
            st.success(
                f"📥 {st.session_state.zengin_filename} をダウンロードしました。"
                "（ブラウザで自動ダウンロードがブロックされた場合は下のボタンを押してください）"
            )

        # 既に生成済みなら、いつでも手動ダウンロードできるボタンも残す
        if st.session_state.zengin_bytes is not None:
            st.download_button(
                label=f"📁 {st.session_state.zengin_filename} を手動ダウンロード",
                data=st.session_state.zengin_bytes,
                file_name=st.session_state.zengin_filename,
                mime="text/plain",
                key="quick_dl_manual",
                use_container_width=True,
            )


def render_item_editor(idx: int, item: dict[str, Any]) -> None:
    """1件分のプレビュー + 編集フォームを左右に並べて表示。"""
    item_id = item["id"]
    inv: InvoiceData = item["invoice"]

    with st.container(border=True):
        header_col, del_col = st.columns([5, 1])
        with header_col:
            matched_badge = "  ✦ マスタ一致" if item.get("matched_payee_key") else ""
            st.markdown(
                f"**#{idx + 1}　{item['filename']}**  ({item['page_count']}ページ){matched_badge}"
            )
        with del_col:
            if st.button("行を削除", key=f"del_{item_id}", use_container_width=True):
                st.session_state["extracted_items"] = [
                    it for it in st.session_state["extracted_items"] if it["id"] != item_id
                ]
                st.session_state.zengin_bytes = None
                save_current_batch_to_db(from_widgets=True)
                st.rerun()

        # マスタから選択（手動上書き）
        conn = get_db()
        all_rows = payee_db.list_all(conn)
        if all_rows:
            master_labels = ["（マスタを使用しない）"] + [
                f"{r['payee_code'] or '-'}  {r['payee_name']}  /  "
                f"{r['bank_name']} {r['branch_name']}  /  {r['account_number']}  "
                f"({r['holder_kana'] or r['holder_name'] or ''})"
                for r in all_rows
            ]
            # 現在マッチ済みなら、その index を初期選択に
            cur_idx = 0
            matched_key = item.get("matched_payee_key")
            if matched_key:
                for i, r in enumerate(all_rows):
                    if r["account_unique_key"] == matched_key:
                        cur_idx = i + 1
                        break
            st.selectbox(
                "マスタから選択（変更すると下のフォームが上書きされます）",
                options=list(range(len(master_labels))),
                format_func=lambda i: master_labels[i],
                index=cur_idx,
                key=f"item_{item_id}_master_sel",
                on_change=on_master_change,
                args=(item_id,),
            )

        preview_col, form_col = st.columns([1.2, 1])

        with preview_col:
            try:
                img = render_pdf_page(item["pdf_bytes"], page_index=0)
                st.image(img, use_container_width=True, caption="1ページ目プレビュー")
            except Exception as e:
                st.warning(f"プレビュー表示に失敗: {e}")

            if item["page_count"] > 1:
                page_n = st.slider(
                    "ページ", 1, item["page_count"], 1,
                    key=f"page_{item_id}",
                )
                if page_n != 1:
                    img2 = render_pdf_page(item["pdf_bytes"], page_index=page_n - 1)
                    st.image(img2, use_container_width=True, caption=f"{page_n}ページ目")

            st.download_button(
                "PDFを開く",
                data=item["pdf_bytes"],
                file_name=item["filename"],
                mime="application/pdf",
                key=f"dl_{item_id}",
                use_container_width=True,
            )

        with form_col:
            st.text_input(
                "受取人名（半角カナ）",
                value=to_halfwidth_kana(inv.payee_name),
                key=f"item_{item_id}_payee",
                help="例: カ)マネーフォワード",
            )

            # 銀行: selectbox (zengin-code 全銀行から検索)
            banks = get_bank_options()
            bank_labels = ["（選択してください）"] + [f"{c}  {n}" for c, n in banks]
            default_bank_idx = resolve_bank_index(inv.bank_code, inv.bank_name)
            bank_index_value = (default_bank_idx + 1) if default_bank_idx is not None else 0

            bank_sel = st.selectbox(
                "銀行",
                options=list(range(len(bank_labels))),
                format_func=lambda i: bank_labels[i],
                index=bank_index_value,
                key=f"item_{item_id}_bank_sel",
                help="銀行名・カナ・コードでタイプ検索できます",
            )

            selected_bank_code = banks[bank_sel - 1][0] if bank_sel > 0 else None

            # 支店: 銀行が選ばれていれば、その銀行の全支店から選択
            if selected_bank_code:
                branches = get_branch_options(selected_bank_code)
                branch_labels = ["（選択してください）"] + [f"{c}  {n}" for c, n in branches]
                default_branch_idx = resolve_branch_index(
                    selected_bank_code, inv.branch_code, inv.branch_name
                )
                branch_index_value = (default_branch_idx + 1) if default_branch_idx is not None else 0

                st.selectbox(
                    "支店",
                    options=list(range(len(branch_labels))),
                    format_func=lambda i: branch_labels[i],
                    index=branch_index_value,
                    key=f"item_{item_id}_branch_sel",
                    help="支店名・カナ・コードでタイプ検索できます",
                )
            else:
                st.selectbox(
                    "支店",
                    options=[0],
                    format_func=lambda i: "（先に銀行を選択してください）",
                    index=0,
                    disabled=True,
                    key=f"item_{item_id}_branch_sel",
                )

            c5, c6 = st.columns([1, 2])
            with c5:
                acc_idx = ACCOUNT_TYPES.index(inv.account_type) if inv.account_type in ACCOUNT_TYPES else 0
                st.selectbox(
                    "種目", ACCOUNT_TYPES,
                    index=acc_idx,
                    key=f"item_{item_id}_acc_type",
                )
            with c6:
                st.text_input(
                    "口座番号", value=inv.account_number,
                    max_chars=7, key=f"item_{item_id}_acc_num",
                )

            st.number_input(
                "金額（円）",
                min_value=0, max_value=10_000_000_000,
                value=int(inv.amount), step=1,
                key=f"item_{item_id}_amount",
            )


def render_batch_manager() -> None:
    """変換タブ上部のバッチ選択・新規作成・リネーム UI。"""
    bconn = get_batch_db()
    batches = batch_db.list_batches(bconn)
    current_id = st.session_state.get("current_batch_id")

    # 現在のバッチ情報
    current_row = None
    if current_id:
        for b in batches:
            if b["batch_id"] == current_id:
                current_row = b
                break

    with st.container(border=True):
        st.markdown("### 振込バッチ")

        if current_row:
            badge = (
                f"**現在のバッチ:** {current_row['name']}　"
                f"（{current_row['item_count']}件 / 合計 {current_row['total_amount']:,}円）"
            )
        else:
            badge = "**現在のバッチ:** *未作成*（PDFを抽出すると自動的に作成されます）"
        st.markdown(badge)

        c1, c2, c3 = st.columns([3, 1, 1])

        with c1:
            # バッチ選択プルダウン（存在する場合）
            if batches:
                options = [b["batch_id"] for b in batches]
                labels = {
                    b["batch_id"]: (
                        f"{b['name']}  —  {b['item_count']}件 / "
                        f"{b['total_amount']:,}円  ({b['updated_at']})"
                    )
                    for b in batches
                }
                # 「（バッチ未選択）」相当のオプションも追加
                options = ["__none__"] + options
                labels["__none__"] = "（バッチ未選択）"
                try:
                    index = options.index(current_id) if current_id in options else 0
                except ValueError:
                    index = 0
                selected = st.selectbox(
                    "過去のバッチを開く",
                    options=options,
                    format_func=lambda x: labels[x],
                    index=index,
                    key="batch_picker",
                )
                if selected != current_id and selected != "__none__":
                    load_batch_into_state(selected)
                    st.rerun()
                elif selected == "__none__" and current_id is not None:
                    clear_item_widget_keys()
                    st.session_state["extracted_items"] = []
                    st.session_state.current_batch_id = None
                    st.session_state.zengin_bytes = None
                    st.rerun()

        with c2:
            if st.button("新規バッチ", use_container_width=True, key="batch_new"):
                clear_item_widget_keys()
                st.session_state["extracted_items"] = []
                st.session_state.current_batch_id = None
                st.session_state.zengin_bytes = None
                st.rerun()

        with c3:
            if st.button(
                "現在の編集を保存",
                use_container_width=True,
                disabled=not current_id,
                key="batch_manual_save",
                type="primary",
            ):
                save_current_batch_to_db(from_widgets=True)
                st.success("保存しました")

        # リネーム / 削除（現在のバッチが選択されている場合のみ）
        if current_row:
            with st.expander("バッチ名の変更・削除", expanded=False):
                new_name = st.text_input(
                    "バッチ名",
                    value=current_row["name"],
                    key=f"batch_rename_{current_id}",
                )
                rc1, rc2 = st.columns([1, 1])
                with rc1:
                    if st.button("名前を保存", use_container_width=True, key="batch_rename_save"):
                        batch_db.rename_batch(bconn, current_id, new_name)
                        st.success("更新しました")
                        st.rerun()
                with rc2:
                    if st.button(
                        "このバッチを削除",
                        use_container_width=True,
                        key="batch_delete",
                    ):
                        batch_db.delete_batch(bconn, current_id)
                        clear_item_widget_keys()
                        st.session_state["extracted_items"] = []
                        st.session_state.current_batch_id = None
                        st.session_state.zengin_bytes = None
                        st.success("削除しました")
                        st.rerun()


def render_convert_tab() -> None:
    cfg = st.session_state.config_data
    claude_cfg = cfg.get("claude", {}) or {}

    # ── 振込バッチ管理 ──
    render_batch_manager()

    st.subheader("1. 請求書PDFをアップロード")
    uploaded = st.file_uploader(
        "PDFファイルを選択（複数可）",
        type=["pdf"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key="uploader",
    )

    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        transfer_type = st.selectbox(
            "振込種別",
            ["総合振込", "給与振込", "賞与振込"],
            index=0,
            key="conv_transfer_type",
        )
    with col2:
        transfer_date = st.text_input(
            "振込日 (MMDD)",
            value=cfg.get("transfer_date", "0101"),
            max_chars=4,
            key="conv_transfer_date",
        )
    with col3:
        output_filename = st.text_input(
            "出力ファイル名",
            value="transfers.txt",
            key="conv_output_filename",
        )

    col_api, col_use = st.columns([3, 1])
    with col_api:
        api_key = st.text_input(
            "Anthropic API Key",
            value=_resolve_api_key(claude_cfg),
            type="password",
            help="Claude APIで請求書を解析するためのキー。config.yaml から自動読み込み。",
            key="conv_api_key",
        )
    with col_use:
        st.write("")
        st.write("")
        use_claude = st.checkbox("Claude APIで抽出", value=True, key="conv_use_claude")

    st.divider()

    st.subheader("2. PDFから振込情報を抽出")
    has_batch = bool(st.session_state.get("current_batch_id"))
    col_run, col_append, col_clear = st.columns([1, 1, 1])
    with col_run:
        run_clicked = st.button(
            "PDF読込・抽出（新規バッチ）",
            type="primary",
            disabled=not uploaded,
            use_container_width=True,
        )
    with col_append:
        append_clicked = st.button(
            "現在のバッチに追加",
            disabled=not (uploaded and has_batch),
            use_container_width=True,
            help="アップロードしたPDFを現在のバッチの末尾に追加します。",
        )
    with col_clear:
        clear_clicked = st.button(
            "全てクリア",
            disabled=not st.session_state["extracted_items"],
            use_container_width=True,
        )

    if clear_clicked:
        # 現在のバッチを保持したまま items だけ空にする → DBの items も空に
        clear_item_widget_keys()
        st.session_state["extracted_items"] = []
        st.session_state.zengin_bytes = None
        save_current_batch_to_db(from_widgets=False)
        st.rerun()

    if run_clicked:
        run_extraction(
            uploaded, api_key, use_claude, claude_cfg.get("model", DEFAULT_MODEL),
            transfer_date=transfer_date, transfer_type=transfer_type, append=False,
        )

    if append_clicked:
        run_extraction(
            uploaded, api_key, use_claude, claude_cfg.get("model", DEFAULT_MODEL),
            transfer_date=transfer_date, transfer_type=transfer_type, append=True,
        )

    if st.session_state["extracted_items"]:
        st.markdown(
            f"**抽出済み: {len(st.session_state["extracted_items"])}件**　— "
            "下記の一覧またはプレビュー横のフォームで修正できます。"
        )

        # 振込情報一覧（編集可）
        render_summary_table()

        # ── ワンクリック生成 + 自動ダウンロード ──
        render_quick_generate(cfg, transfer_type, transfer_date, output_filename)

        for idx, item in enumerate(st.session_state["extracted_items"]):
            render_item_editor(idx, item)

        st.divider()

        st.subheader("3. 全銀ファイルを生成")
        if st.button("全銀ファイル生成", type="primary"):
            run_generation(
                cfg,
                transfer_type,
                transfer_date,
                output_filename,
            )

        if st.session_state.zengin_bytes is not None:
            st.success(f"生成完了: {st.session_state.zengin_filename}")
            st.download_button(
                label="ダウンロード",
                data=st.session_state.zengin_bytes,
                file_name=st.session_state.zengin_filename,
                mime="text/plain",
                key="dl_zengin",
            )


def run_extraction(
    uploaded_files,
    api_key: str,
    use_claude: bool,
    model: str,
    transfer_date: str = "",
    transfer_type: str = "総合振込",
    append: bool = False,
) -> None:
    """アップロードされたPDFを順に抽出し、items に追加。

    append=False の場合:
        既存の items をクリアし、新しいバッチを自動作成して DB に永続化する。
    append=True の場合:
        現在のバッチに追加する（items は維持して末尾に追加）。
    """
    import fitz

    # マスタから既知の取引先ヒントを構築（重複排除）
    conn = get_db()
    seen_payees: set[str] = set()
    payee_hints: list[dict] = []
    for r in payee_db.list_all(conn):
        name = (r["payee_name"] or "").strip()
        if not name or name in seen_payees:
            continue
        seen_payees.add(name)
        payee_hints.append({
            "name": name,
            "kana": (r["holder_kana"] or r["payee_name_kana"] or "").strip(),
        })

    new_items: list[dict[str, Any]] = []
    progress = st.progress(0.0, text="処理を開始しています...")
    log_area = st.expander("処理ログ", expanded=True)
    logs: list[str] = []
    if payee_hints:
        logs.append(f"マスタヒント: {len(payee_hints)} 件をプロンプトに追加")

    total = len(uploaded_files)
    for i, f in enumerate(uploaded_files, 1):
        progress.progress((i - 1) / total, text=f"処理中 ({i}/{total}): {f.name}")
        pdf_bytes = f.read()

        # ページ数取得
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            page_count = len(doc)
            doc.close()
        except Exception:
            page_count = 1

        # 抽出（既存ロジックは Path を取るので tmp に書き出す）
        invoice: InvoiceData | None = None
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)
        try:
            pdf_content = read_pdf(tmp_path)
            logs.append(f"✓ {f.name}: {pdf_content.extraction_method} ({pdf_content.page_count}ページ)")
            invoice = extract_invoice(
                pdf_content,
                model=model,
                api_key=api_key or None,
                use_claude=use_claude,
                payee_hints=payee_hints,
            )
            logs.append(
                f"   → {to_halfwidth_kana(invoice.payee_name)} / "
                f"{invoice.bank_name or '?'} {invoice.branch_name or '?'} / "
                f"{invoice.account_type} {invoice.account_number} / "
                f"{invoice.amount:,}円"
            )
        except Exception as e:
            logs.append(f"✗ {f.name}: {e}")
            # 抽出失敗でも空のInvoiceで枠だけ作って、手動入力できるようにする
            invoice = InvoiceData(
                payee_name="",
                account_type="普通",
                account_number="",
                amount=0,
            )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

        new_items.append({
            "id": uuid.uuid4().hex,
            "filename": f.name,
            "pdf_bytes": pdf_bytes,
            "invoice": invoice,
            "page_count": page_count,
            "matched_payee_key": None,
        })

    progress.progress(1.0, text=f"完了: {total} 件処理")

    # ── マスタ自動マッチング ──
    if payee_db.count(conn) > 0:
        matched = 0
        for it in new_items:
            inv = it["invoice"]
            row = payee_db.find_match(
                conn,
                account_number=inv.account_number or None,
                holder_name=inv.payee_name or None,
                payee_name=inv.payee_name or None,
            )
            if row:
                amount = inv.amount  # 金額は抽出値を維持
                it["invoice"] = invoice_from_payee_row(row, fallback_amount=amount)
                it["matched_payee_key"] = row["account_unique_key"]
                matched += 1
                logs.append(
                    f"   ✦ マスタ一致: {row['payee_name']} / "
                    f"{row['bank_name']} {row['branch_name']} {row['account_number']}"
                )
        if matched:
            logs.append(f"マスタ自動マッチ: {matched} / {len(new_items)} 件")

    with log_area:
        for line in logs:
            st.write(line)

    if new_items:
        bconn = get_batch_db()

        if append and st.session_state.get("current_batch_id"):
            # 既存バッチに追加: widget の最新値を items["invoice"] に取り込んでから末尾に追加
            existing = st.session_state.get("extracted_items", [])
            for it in existing:
                if f"item_{it['id']}_payee" in st.session_state:
                    it["invoice"] = build_invoice_from_widgets(it["id"])
            combined = existing + new_items
            clear_item_widget_keys()
            st.session_state["extracted_items"] = combined
            st.session_state.zengin_bytes = None
            save_current_batch_to_db(from_widgets=False)
            batch_row = batch_db.get_batch(bconn, st.session_state.current_batch_id)
            st.toast(
                f"📥 {len(new_items)} 件追加: {batch_row['name']}（合計 {len(combined)}件）",
                icon="✅",
            )
        else:
            # 新規バッチを作成
            clear_item_widget_keys()
            st.session_state["extracted_items"] = new_items
            st.session_state.zengin_bytes = None
            batch_id = batch_db.create_batch(
                bconn, transfer_date=transfer_date or None, transfer_type=transfer_type
            )
            st.session_state.current_batch_id = batch_id
            save_current_batch_to_db(from_widgets=False)
            batch_row = batch_db.get_batch(bconn, batch_id)
            st.toast(f"💾 バッチ保存: {batch_row['name']}", icon="✅")

        st.rerun()


def run_generation(
    cfg: dict,
    transfer_type: str,
    transfer_date: str,
    output_filename: str,
) -> None:
    consignor = cfg.get("consignor", {}) or {}
    source = cfg.get("source", {}) or {}

    missing = []
    for key, label in [("code", "委託者コード"), ("name", "委託者名")]:
        if not consignor.get(key):
            missing.append(f"振込元設定の「{label}」")
    for key, label in [
        ("bank_code", "仕向銀行コード"),
        ("bank_name", "仕向銀行名"),
        ("branch_code", "仕向支店コード"),
        ("branch_name", "仕向支店名"),
        ("account_number", "口座番号"),
    ]:
        if not source.get(key):
            missing.append(f"振込元設定の「{label}」")

    if missing:
        st.error("振込元設定が未入力です: " + ", ".join(missing) + "（「振込元設定」タブで入力してください）")
        return

    # 各itemの編集後ウィジェット値からInvoiceDataを構築
    invoices: list[InvoiceData] = []
    errors: list[str] = []
    for idx, item in enumerate(st.session_state["extracted_items"]):
        try:
            inv = build_invoice_from_widgets(item["id"])
            if not inv.account_number or inv.amount <= 0:
                errors.append(f"#{idx + 1} ({item['filename']}): 口座番号または金額が未入力")
                continue
            invoices.append(inv)
        except Exception as e:
            errors.append(f"#{idx + 1} ({item['filename']}): {e}")

    if errors:
        st.error("入力エラーがあります:\n- " + "\n- ".join(errors))
        return

    if not invoices:
        st.error("有効な振込データがありません")
        return

    # 編集後の最新状態をDBに反映 + バッチのメタも更新
    bconn = get_batch_db()
    save_current_batch_to_db(from_widgets=True)
    if st.session_state.get("current_batch_id"):
        batch_db.update_batch_meta(
            bconn, st.session_state.current_batch_id,
            transfer_date=transfer_date, transfer_type=transfer_type,
        )

    config = ConsignorConfig(
        consignor_code=consignor["code"],
        consignor_name=consignor["name"],
        bank_code=source["bank_code"],
        bank_name=source["bank_name"],
        branch_code=source["branch_code"],
        branch_name=source["branch_name"],
        account_type=source.get("account_type", "1"),
        account_number=source["account_number"],
        transfer_date=transfer_date,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / output_filename
        try:
            written = generate_zengin(invoices, config, out_path, transfer_type=transfer_type)
        except ValueError as e:
            st.error(f"生成失敗: {e}")
            return
        st.session_state.zengin_bytes = written.read_bytes()
        st.session_state.zengin_filename = written.name


def render_master_tab() -> None:
    """取引先マスタ管理タブ。"""
    import pandas as pd

    conn = get_db()
    total = payee_db.count(conn)
    st.subheader(f"取引先マスタ（登録 {total} 件）")

    # ── CSV取り込み ──
    with st.expander("MoneyForward CSV を取り込む", expanded=(total == 0)):
        st.caption(
            "MoneyForwardの「取引先・取引先口座・支払先」エクスポートCSV（UTF-8）に対応。"
            "口座未登録の取引先行はスキップされます。"
        )
        upload = st.file_uploader(
            "CSVを選択",
            type=["csv"],
            key="master_csv_uploader",
            label_visibility="collapsed",
        )
        mode = st.radio(
            "取り込みモード",
            ["既存に追加（同じ口座は上書き）", "全削除して入れ替え"],
            index=0,
            horizontal=True,
            key="master_csv_mode",
        )
        if st.button(
            "取り込み実行",
            type="primary",
            disabled=not upload,
            key="master_csv_run",
        ):
            replace = mode.startswith("全削除")
            try:
                upserted, skipped = payee_db.import_mf_csv(conn, upload.getvalue(), replace=replace)
                st.success(f"取り込み完了: {upserted} 件（スキップ {skipped} 件）")
                st.rerun()
            except Exception as e:
                st.error(f"取り込み失敗: {e}")

    # ── 検索 ──
    q = st.text_input(
        "検索（取引先名・カナ・名義人・銀行・支店・口座番号・取引先コード）",
        key="master_search",
    )

    rows = payee_db.search(conn, q) if q.strip() else payee_db.list_all(conn)

    if not rows:
        st.info("登録されたマスタがありません。CSVを取り込むか、下の『手動追加』から登録してください。")
    else:
        df = pd.DataFrame([dict(r) for r in rows])
        display_cols = [
            "payee_code", "payee_name", "payee_name_kana",
            "bank_name", "bank_code", "branch_name", "branch_code",
            "account_type", "account_number", "holder_name", "holder_kana",
            "note", "account_unique_key",
        ]
        display_cols = [c for c in display_cols if c in df.columns]
        display_df = df[display_cols].rename(columns={
            "payee_code": "取引先コード",
            "payee_name": "取引先名",
            "payee_name_kana": "取引先カナ",
            "bank_name": "銀行",
            "bank_code": "銀行C",
            "branch_name": "支店",
            "branch_code": "支店C",
            "account_type": "種目",
            "account_number": "口座番号",
            "holder_name": "名義人",
            "holder_kana": "名義人カナ",
            "note": "備考",
            "account_unique_key": "ID",
        })
        st.dataframe(display_df, use_container_width=True, hide_index=True, height=400)

        # 1件選んで編集・削除
        st.markdown("**個別編集・削除**")
        labels = [
            f"{r['payee_code'] or '-'}  {r['payee_name']}  /  {r['bank_name']} {r['branch_name']}  /  {r['account_number']}"
            for r in rows
        ]
        idx = st.selectbox(
            "対象を選択",
            options=list(range(len(rows))),
            format_func=lambda i: labels[i],
            key="master_select_idx",
        )
        target = rows[idx]
        target_key = target["account_unique_key"]
        prefix = f"edit_{target_key}"

        c1, c2 = st.columns(2)
        with c1:
            e_payee_code = st.text_input("取引先コード", value=target["payee_code"] or "", key=f"{prefix}_payee_code")
            e_payee_name = st.text_input("取引先名", value=target["payee_name"] or "", key=f"{prefix}_payee_name")
            e_payee_kana = st.text_input("取引先カナ", value=target["payee_name_kana"] or "", key=f"{prefix}_payee_kana")

            # 銀行 selectbox（コードは自動表示）
            banks = get_bank_options()
            bank_labels = ["（選択してください）"] + [f"{c}  {n}" for c, n in banks]
            default_bank_idx = resolve_bank_index(target["bank_code"], target["bank_name"])
            bank_idx_value = (default_bank_idx + 1) if default_bank_idx is not None else 0
            bank_sel = st.selectbox(
                "銀行",
                options=list(range(len(bank_labels))),
                format_func=lambda i: bank_labels[i],
                index=bank_idx_value,
                key=f"{prefix}_bank_sel",
            )
            sel_bank_code, sel_bank_name = ("", "")
            if bank_sel > 0:
                sel_bank_code, sel_bank_name = banks[bank_sel - 1]
            st.text_input(
                "銀行コード（自動）", value=sel_bank_code, disabled=True,
                key=f"{prefix}_bank_code_display",
            )

            # 支店 selectbox（コードは自動表示）
            if sel_bank_code:
                branches = get_branch_options(sel_bank_code)
                branch_labels = ["（選択してください）"] + [f"{c}  {n}" for c, n in branches]
                default_branch_idx = resolve_branch_index(
                    sel_bank_code, target["branch_code"], target["branch_name"]
                )
                branch_idx_value = (default_branch_idx + 1) if default_branch_idx is not None else 0
                branch_sel = st.selectbox(
                    "支店",
                    options=list(range(len(branch_labels))),
                    format_func=lambda i: branch_labels[i],
                    index=branch_idx_value,
                    key=f"{prefix}_branch_sel",
                )
                sel_branch_code, sel_branch_name = ("", "")
                if branch_sel > 0:
                    sel_branch_code, sel_branch_name = branches[branch_sel - 1]
            else:
                sel_branch_code, sel_branch_name = "", ""
                st.selectbox(
                    "支店",
                    options=[0],
                    format_func=lambda i: "（先に銀行を選択してください）",
                    index=0,
                    disabled=True,
                    key=f"{prefix}_branch_sel_disabled",
                )
            st.text_input(
                "支店コード（自動）", value=sel_branch_code, disabled=True,
                key=f"{prefix}_branch_code_display",
            )
        with c2:
            e_acc_type = st.selectbox(
                "種目",
                ACCOUNT_TYPES,
                index=ACCOUNT_TYPES.index(target["account_type"]) if target["account_type"] in ACCOUNT_TYPES else 0,
                key=f"{prefix}_acc_type",
            )
            e_acc_num = st.text_input("口座番号", value=target["account_number"] or "", max_chars=7, key=f"{prefix}_acc_num")
            e_holder_name = st.text_input("名義人", value=target["holder_name"] or "", key=f"{prefix}_holder_name")
            e_holder_kana = st.text_input("名義人カナ", value=target["holder_kana"] or "", key=f"{prefix}_holder_kana")
            e_note = st.text_area("備考", value=target["note"] or "", height=80, key=f"{prefix}_note")

        fc1, fc2 = st.columns(2)
        with fc1:
            save = st.button("保存", type="primary", use_container_width=True, key=f"{prefix}_save")
        with fc2:
            delete = st.button("この行を削除", use_container_width=True, key=f"{prefix}_delete")

        if save:
            if not sel_bank_code or not sel_branch_code:
                st.error("銀行と支店をプルダウンから選択してください。")
            else:
                payee_db.update(conn, target_key, {
                    "payee_code": e_payee_code,
                    "payee_name": e_payee_name,
                    "payee_name_kana": e_payee_kana,
                    "bank_name": sel_bank_name,
                    "bank_code": sel_bank_code,
                    "branch_name": sel_branch_name,
                    "branch_code": sel_branch_code,
                    "account_type": e_acc_type,
                    "account_number": e_acc_num,
                    "holder_name": e_holder_name,
                    "holder_kana": e_holder_kana,
                    "note": e_note,
                    "payee_unique_key": target["payee_unique_key"] or "",
                })
                st.success("保存しました")
                st.rerun()
        if delete:
            payee_db.delete(conn, target_key)
            conn.commit()
            st.success("削除しました")
            st.rerun()

    # ── 手動追加 ──
    with st.expander("手動でマスタを追加", expanded=False):
        prefix = "add"
        c1, c2 = st.columns(2)
        with c1:
            a_payee_code = st.text_input("取引先コード", key=f"{prefix}_payee_code")
            a_payee_name = st.text_input("取引先名 *", key=f"{prefix}_payee_name")
            a_payee_kana = st.text_input("取引先カナ", key=f"{prefix}_payee_kana")

            # 銀行 selectbox（コードは自動表示）
            banks = get_bank_options()
            bank_labels = ["（選択してください）"] + [f"{c}  {n}" for c, n in banks]
            a_bank_sel = st.selectbox(
                "銀行 *",
                options=list(range(len(bank_labels))),
                format_func=lambda i: bank_labels[i],
                index=0,
                key=f"{prefix}_bank_sel",
            )
            a_sel_bank_code, a_sel_bank_name = ("", "")
            if a_bank_sel > 0:
                a_sel_bank_code, a_sel_bank_name = banks[a_bank_sel - 1]
            st.text_input(
                "銀行コード（自動）", value=a_sel_bank_code, disabled=True,
                key=f"{prefix}_bank_code_display",
            )

            # 支店 selectbox（コードは自動表示）
            if a_sel_bank_code:
                branches = get_branch_options(a_sel_bank_code)
                branch_labels = ["（選択してください）"] + [f"{c}  {n}" for c, n in branches]
                a_branch_sel = st.selectbox(
                    "支店 *",
                    options=list(range(len(branch_labels))),
                    format_func=lambda i: branch_labels[i],
                    index=0,
                    key=f"{prefix}_branch_sel",
                )
                a_sel_branch_code, a_sel_branch_name = ("", "")
                if a_branch_sel > 0:
                    a_sel_branch_code, a_sel_branch_name = branches[a_branch_sel - 1]
            else:
                a_sel_branch_code, a_sel_branch_name = "", ""
                st.selectbox(
                    "支店 *",
                    options=[0],
                    format_func=lambda i: "（先に銀行を選択してください）",
                    index=0,
                    disabled=True,
                    key=f"{prefix}_branch_sel_disabled",
                )
            st.text_input(
                "支店コード（自動）", value=a_sel_branch_code, disabled=True,
                key=f"{prefix}_branch_code_display",
            )
        with c2:
            a_acc_type = st.selectbox("種目", ACCOUNT_TYPES, index=0, key=f"{prefix}_acc_type")
            a_acc_num = st.text_input("口座番号 *", max_chars=7, key=f"{prefix}_acc_num")
            a_holder_name = st.text_input("名義人", key=f"{prefix}_holder_name")
            a_holder_kana = st.text_input("名義人カナ", key=f"{prefix}_holder_kana")
            a_note = st.text_area("備考", height=80, key=f"{prefix}_note")

        if st.button("追加", type="primary", key=f"{prefix}_submit"):
            missing = []
            if not a_payee_name: missing.append("取引先名")
            if not a_sel_bank_code: missing.append("銀行")
            if not a_sel_branch_code: missing.append("支店")
            if not a_acc_num: missing.append("口座番号")
            if missing:
                st.error("未入力: " + ", ".join(missing))
            else:
                payee_db.insert_manual(conn, {
                    "payee_code": a_payee_code,
                    "payee_name": a_payee_name,
                    "payee_name_kana": a_payee_kana,
                    "bank_name": a_sel_bank_name,
                    "bank_code": a_sel_bank_code,
                    "branch_name": a_sel_branch_name,
                    "branch_code": a_sel_branch_code,
                    "account_type": a_acc_type,
                    "account_number": a_acc_num,
                    "holder_name": a_holder_name,
                    "holder_kana": a_holder_kana,
                    "note": a_note,
                })
                # 入力フィールドをクリアして次の追加に備える
                for k in [
                    f"{prefix}_payee_code", f"{prefix}_payee_name", f"{prefix}_payee_kana",
                    f"{prefix}_bank_sel", f"{prefix}_branch_sel",
                    f"{prefix}_acc_num", f"{prefix}_holder_name", f"{prefix}_holder_kana",
                    f"{prefix}_note",
                ]:
                    if k in st.session_state:
                        del st.session_state[k]
                st.success("追加しました")
                st.rerun()

    # ── 全件削除 ──
    if total > 0:
        with st.expander("⚠ 全件削除", expanded=False):
            confirm = st.checkbox(f"マスタ {total} 件を全て削除する（取り消し不可）", key="master_delete_all_confirm")
            if st.button("全削除実行", disabled=not confirm, type="primary", key="master_delete_all_btn"):
                n = payee_db.delete_all(conn)
                conn.commit()
                st.success(f"削除しました: {n} 件")
                st.rerun()


def render_config_tab() -> None:
    cfg = st.session_state.config_data
    consignor = cfg.get("consignor", {}) or {}
    source = cfg.get("source", {}) or {}
    claude_cfg = cfg.get("claude", {}) or {}

    st.subheader("振込元（委託者）情報")
    col1, col2 = st.columns(2)
    with col1:
        new_consignor_code = st.text_input("委託者コード (10桁)", value=consignor.get("code", ""), key="cfg_consignor_code")
        new_bank_code = st.text_input("仕向銀行コード (4桁)", value=source.get("bank_code", ""), key="cfg_bank_code")
        new_branch_code = st.text_input("仕向支店コード (3桁)", value=source.get("branch_code", ""), key="cfg_branch_code")
        new_account_type = st.selectbox(
            "預金種目",
            options=["1", "2"],
            index=0 if source.get("account_type", "1") == "1" else 1,
            format_func=lambda x: {"1": "1: 普通", "2": "2: 当座"}.get(x, x),
            key="cfg_account_type",
        )
    with col2:
        new_consignor_name = st.text_input("委託者名 (半角カナ)", value=consignor.get("name", ""), key="cfg_consignor_name")
        new_bank_name = st.text_input("仕向銀行名 (半角カナ)", value=source.get("bank_name", ""), key="cfg_bank_name")
        new_branch_name = st.text_input("仕向支店名 (半角カナ)", value=source.get("branch_name", ""), key="cfg_branch_name")
        new_account_number = st.text_input("口座番号 (7桁)", value=source.get("account_number", ""), key="cfg_account_number")

    st.subheader("既定の振込日")
    new_transfer_date = st.text_input(
        "振込日 (MMDD)", value=cfg.get("transfer_date", "0101"), max_chars=4,
        key="cfg_transfer_date",
    )

    st.subheader("Claude API")
    new_model = st.text_input("モデル", value=claude_cfg.get("model", DEFAULT_MODEL), key="cfg_model")
    new_api_key = st.text_input(
        "APIキー (config.yamlに平文保存されます)",
        value=claude_cfg.get("api_key", ""),
        type="password",
        key="cfg_api_key",
    )

    st.divider()
    if st.button("config.yaml に保存", type="primary"):
        new_cfg = {
            "consignor": {
                "code": new_consignor_code,
                "name": new_consignor_name,
            },
            "source": {
                "bank_code": new_bank_code,
                "bank_name": new_bank_name,
                "branch_code": new_branch_code,
                "branch_name": new_branch_name,
                "account_type": new_account_type,
                "account_number": new_account_number,
            },
            "transfer_date": new_transfer_date,
            "claude": {
                "model": new_model,
                "api_key": new_api_key,
            },
        }
        save_config(new_cfg)
        st.session_state.config_data = new_cfg
        st.success(f"保存しました: {CONFIG_PATH}")


def main() -> None:
    st.set_page_config(page_title="全銀フォーマット変換ツール", page_icon="🏦", layout="wide")
    init_state()

    st.title("全銀フォーマット変換ツール")
    st.caption("請求書PDFを Claude API で解析し、全銀フォーマット (.txt) を生成します。")

    tab_convert, tab_master, tab_config = st.tabs(["変換", "取引先マスタ", "振込元設定"])
    with tab_convert:
        render_convert_tab()
    with tab_master:
        render_master_tab()
    with tab_config:
        render_config_tab()


if __name__ == "__main__":
    main()
