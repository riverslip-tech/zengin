"""全銀フォーマット変換ツール GUI"""

import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from typing import Optional

import yaml

from .models import InvoiceData, ConsignorConfig
from .pdf_reader import read_pdf
from .extractor import extract_invoice
from .kana_utils import to_halfwidth_kana
from .zengin_writer import generate_zengin


class ZenginConverterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("全銀フォーマット変換ツール")
        self.root.geometry("900x700")
        self.root.minsize(800, 600)

        self.pdf_files: list[Path] = []
        self.invoices: list[InvoiceData] = []

        self._load_config()
        # API キー: config.yaml > 環境変数 の優先順
        self.api_key = (
            self.config_data.get("claude", {}).get("api_key", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        self._build_ui()

    def _load_config(self):
        """config.yaml を読み込む"""
        self.config_data = {}
        # exe と同じフォルダ、カレントディレクトリ、ソースツリーの順に探す
        exe_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(".")
        candidates = [
            exe_dir / "config.yaml",
            Path("config.yaml"),
            Path(__file__).parent.parent.parent / "config.yaml",
        ]
        for p in candidates:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    self.config_data = yaml.safe_load(f) or {}
                self.config_path = p
                break

    def _build_ui(self):
        """UI構築"""
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # タブ1: メイン（PDF読込・変換）
        main_frame = ttk.Frame(notebook)
        notebook.add(main_frame, text=" 変換 ")
        self._build_main_tab(main_frame)

        # タブ2: 振込元設定
        config_frame = ttk.Frame(notebook)
        notebook.add(config_frame, text=" 振込元設定 ")
        self._build_config_tab(config_frame)

    # ── メインタブ ──

    def _build_main_tab(self, parent):
        # 上部: PDF選択
        top = ttk.LabelFrame(parent, text="請求書PDF", padding=5)
        top.pack(fill=tk.X, padx=5, pady=5)

        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill=tk.X)

        ttk.Button(btn_frame, text="PDF追加...", command=self._add_pdfs).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="選択削除", command=self._remove_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="全削除", command=self._clear_pdfs).pack(side=tk.LEFT, padx=2)

        self.pdf_listbox = tk.Listbox(top, height=4, selectmode=tk.EXTENDED)
        self.pdf_listbox.pack(fill=tk.X, pady=2)

        # 中部: 振込日・出力先
        mid = ttk.Frame(parent)
        mid.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(mid, text="振込種別:").pack(side=tk.LEFT)
        self.transfer_type_var = tk.StringVar(value="総合振込")
        transfer_type_combo = ttk.Combobox(
            mid, textvariable=self.transfer_type_var, width=10,
            values=["総合振込", "給与振込", "賞与振込"], state="readonly"
        )
        transfer_type_combo.pack(side=tk.LEFT, padx=5)

        ttk.Label(mid, text="振込日(MMDD):").pack(side=tk.LEFT, padx=(10, 0))
        self.transfer_date_var = tk.StringVar(
            value=self.config_data.get("transfer_date", "0101")
        )
        ttk.Entry(mid, textvariable=self.transfer_date_var, width=6).pack(side=tk.LEFT, padx=5)

        ttk.Label(mid, text="出力先:").pack(side=tk.LEFT, padx=(20, 0))
        self.output_var = tk.StringVar(value="output/transfers.txt")
        ttk.Entry(mid, textvariable=self.output_var, width=30).pack(side=tk.LEFT, padx=5)
        ttk.Button(mid, text="...", width=3, command=self._browse_output).pack(side=tk.LEFT)

        # API キー
        api_frame = ttk.Frame(parent)
        api_frame.pack(fill=tk.X, padx=5, pady=2)
        ttk.Label(api_frame, text="API Key:").pack(side=tk.LEFT)
        self.api_key_var = tk.StringVar(value=self.api_key)
        ttk.Entry(api_frame, textvariable=self.api_key_var, width=50, show="*").pack(side=tk.LEFT, padx=5)

        self.use_claude_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            api_frame, text="Claude APIで抽出", variable=self.use_claude_var
        ).pack(side=tk.LEFT, padx=5)

        # 実行ボタン
        btn_row = ttk.Frame(parent)
        btn_row.pack(fill=tk.X, padx=5, pady=5)

        self.extract_btn = ttk.Button(
            btn_row, text="1. PDF読込・抽出", command=self._run_extract
        )
        self.extract_btn.pack(side=tk.LEFT, padx=2)

        self.convert_btn = ttk.Button(
            btn_row, text="2. 全銀ファイル生成", command=self._run_convert, state=tk.DISABLED
        )
        self.convert_btn.pack(side=tk.LEFT, padx=2)

        self.progress = ttk.Progressbar(btn_row, mode="indeterminate", length=150)
        self.progress.pack(side=tk.LEFT, padx=10)

        # 抽出結果テーブル
        table_frame = ttk.LabelFrame(parent, text="抽出結果", padding=5)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        columns = ("file", "payee", "bank", "branch", "type", "account", "amount")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=6)

        self.tree.heading("file", text="ファイル")
        self.tree.heading("payee", text="受取人名")
        self.tree.heading("bank", text="銀行")
        self.tree.heading("branch", text="支店")
        self.tree.heading("type", text="種目")
        self.tree.heading("account", text="口座番号")
        self.tree.heading("amount", text="金額")

        self.tree.column("file", width=120)
        self.tree.column("payee", width=150)
        self.tree.column("bank", width=100)
        self.tree.column("branch", width=80)
        self.tree.column("type", width=50)
        self.tree.column("account", width=80)
        self.tree.column("amount", width=90, anchor=tk.E)

        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # ダブルクリックで編集
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        # テーブル下の操作ボタン
        table_btn_frame = ttk.Frame(parent)
        table_btn_frame.pack(fill=tk.X, padx=5)
        ttk.Button(table_btn_frame, text="選択行を編集", command=self._edit_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(table_btn_frame, text="選択行を削除", command=self._delete_selected).pack(side=tk.LEFT, padx=2)

        # ログ
        log_frame = ttk.LabelFrame(parent, text="ログ", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        log_btn_frame = ttk.Frame(log_frame)
        log_btn_frame.pack(fill=tk.X)
        ttk.Button(log_btn_frame, text="ログクリア", command=self._clear_log).pack(side=tk.RIGHT)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=6, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ── 設定タブ ──

    def _build_config_tab(self, parent):
        consignor = self.config_data.get("consignor", {})
        source = self.config_data.get("source", {})

        fields_frame = ttk.LabelFrame(parent, text="振込元（委託者）情報", padding=10)
        fields_frame.pack(fill=tk.X, padx=10, pady=10)

        self.config_vars = {}
        rows = [
            ("consignor_code", "委託者コード (10桁)", consignor.get("code", "")),
            ("consignor_name", "委託者名 (半角カナ)", consignor.get("name", "")),
            ("bank_code", "銀行コード (4桁)", source.get("bank_code", "")),
            ("bank_name", "銀行名 (半角カナ)", source.get("bank_name", "")),
            ("branch_code", "支店コード (3桁)", source.get("branch_code", "")),
            ("branch_name", "支店名 (半角カナ)", source.get("branch_name", "")),
            ("account_type", "預金種目 (1:普通 2:当座)", source.get("account_type", "1")),
            ("account_number", "口座番号 (7桁)", source.get("account_number", "")),
        ]

        for i, (key, label, default) in enumerate(rows):
            ttk.Label(fields_frame, text=label).grid(row=i, column=0, sticky=tk.W, pady=2)
            var = tk.StringVar(value=default)
            self.config_vars[key] = var
            ttk.Entry(fields_frame, textvariable=var, width=30).grid(
                row=i, column=1, sticky=tk.W, padx=10, pady=2
            )

        ttk.Button(
            parent, text="設定を保存 (config.yaml)", command=self._save_config
        ).pack(padx=10, pady=10, anchor=tk.W)

    # ── テーブル編集 ──

    def _on_tree_double_click(self, event):
        self._edit_selected()

    def _edit_selected(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("情報", "編集する行を選択してください")
            return
        item_id = selected[0]
        idx = self.tree.index(item_id)
        if idx >= len(self.invoices):
            return
        invoice = self.invoices[idx]
        EditDialog(self.root, invoice, self._on_edit_done, idx, item_id)

    def _on_edit_done(self, idx: int, item_id: str, updated: InvoiceData):
        self.invoices[idx] = updated
        self.tree.item(item_id, values=(
            self.tree.item(item_id, "values")[0],  # ファイル名はそのまま
            to_halfwidth_kana(updated.payee_name),
            updated.bank_name or "",
            updated.branch_name or "",
            updated.account_type,
            updated.account_number,
            f"{updated.amount:,}",
        ))
        self._log(f"編集完了: {to_halfwidth_kana(updated.payee_name)} {updated.amount:,}円")

    def _delete_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        for item_id in reversed(selected):
            idx = self.tree.index(item_id)
            if idx < len(self.invoices):
                del self.invoices[idx]
            self.tree.delete(item_id)
        if not self.invoices:
            self.convert_btn.configure(state=tk.DISABLED)

    # ── アクション ──

    def _log(self, msg: str):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _add_pdfs(self):
        files = filedialog.askopenfilenames(
            title="請求書PDFを選択",
            filetypes=[("PDF", "*.pdf"), ("All", "*.*")],
        )
        for f in files:
            p = Path(f)
            if p not in self.pdf_files:
                self.pdf_files.append(p)
                self.pdf_listbox.insert(tk.END, p.name)

    def _remove_selected(self):
        indices = list(self.pdf_listbox.curselection())
        for i in reversed(indices):
            self.pdf_listbox.delete(i)
            del self.pdf_files[i]

    def _clear_pdfs(self):
        self.pdf_listbox.delete(0, tk.END)
        self.pdf_files.clear()
        self.invoices.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.convert_btn.configure(state=tk.DISABLED)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="出力先を選択",
            defaultextension=".txt",
            filetypes=[("全銀ファイル", "*.txt"), ("All", "*.*")],
        )
        if path:
            self.output_var.set(path)

    def _run_extract(self):
        if not self.pdf_files:
            messagebox.showwarning("警告", "PDFファイルを追加してください")
            return

        self.extract_btn.configure(state=tk.DISABLED)
        self.convert_btn.configure(state=tk.DISABLED)
        self.progress.start()
        self.invoices.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)

        threading.Thread(target=self._extract_worker, daemon=True).start()

    def _extract_worker(self):
        api_key = self.api_key_var.get().strip() or None
        use_claude = self.use_claude_var.get()
        model = self.config_data.get("claude", {}).get("model", "claude-sonnet-4-6")

        for pdf_path in self.pdf_files:
            self.root.after(0, self._log, f"処理中: {pdf_path.name}")
            try:
                pdf_content = read_pdf(pdf_path)
                invoice = extract_invoice(
                    pdf_content, model=model, api_key=api_key, use_claude=use_claude
                )
                self.invoices.append(invoice)

                # テーブルに追加
                self.root.after(0, self._add_tree_row, pdf_path.name, invoice)
                self.root.after(0, self._log, f"  OK: {to_halfwidth_kana(invoice.payee_name)} {invoice.amount:,}円")
            except Exception as e:
                self.root.after(0, self._log, f"  エラー: {e}")

        self.root.after(0, self._extract_done)

    def _add_tree_row(self, filename: str, inv: InvoiceData):
        self.tree.insert("", tk.END, values=(
            filename,
            to_halfwidth_kana(inv.payee_name),
            inv.bank_name or "",
            inv.branch_name or "",
            inv.account_type,
            inv.account_number,
            f"{inv.amount:,}",
        ))

    def _extract_done(self):
        self.progress.stop()
        self.extract_btn.configure(state=tk.NORMAL)
        if self.invoices:
            self.convert_btn.configure(state=tk.NORMAL)
            total = sum(inv.amount for inv in self.invoices)
            self._log(f"抽出完了: {len(self.invoices)}件, 合計 {total:,}円")
        else:
            self._log("抽出できた請求書がありません")

    def _run_convert(self):
        if not self.invoices:
            messagebox.showwarning("警告", "先にPDFを読み込んでください")
            return

        try:
            config = ConsignorConfig(
                consignor_code=self.config_vars["consignor_code"].get(),
                consignor_name=self.config_vars["consignor_name"].get(),
                bank_code=self.config_vars["bank_code"].get(),
                bank_name=self.config_vars["bank_name"].get(),
                branch_code=self.config_vars["branch_code"].get(),
                branch_name=self.config_vars["branch_name"].get(),
                account_type=self.config_vars["account_type"].get(),
                account_number=self.config_vars["account_number"].get(),
                transfer_date=self.transfer_date_var.get(),
            )
        except Exception as e:
            messagebox.showerror("設定エラー", str(e))
            return

        output_path = self.output_var.get()
        try:
            result = generate_zengin(
                self.invoices, config, output_path,
                transfer_type=self.transfer_type_var.get(),
            )
            self._log(f"全銀ファイル生成完了: {result}")
            messagebox.showinfo("完了", f"全銀ファイルを生成しました\n{result}")
        except Exception as e:
            self._log(f"生成エラー: {e}")
            messagebox.showerror("エラー", str(e))

    def _save_config(self):
        data = {
            "consignor": {
                "code": self.config_vars["consignor_code"].get(),
                "name": self.config_vars["consignor_name"].get(),
            },
            "source": {
                "bank_code": self.config_vars["bank_code"].get(),
                "bank_name": self.config_vars["bank_name"].get(),
                "branch_code": self.config_vars["branch_code"].get(),
                "branch_name": self.config_vars["branch_name"].get(),
                "account_type": self.config_vars["account_type"].get(),
                "account_number": self.config_vars["account_number"].get(),
            },
            "transfer_date": self.transfer_date_var.get(),
            "claude": {
                "model": self.config_data.get("claude", {}).get("model", "claude-sonnet-4-6"),
                "api_key": self.api_key_var.get().strip(),
            },
        }

        config_path = getattr(self, "config_path", Path("config.yaml"))
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

        self._log(f"設定を保存しました: {config_path}")
        self.config_data = data
        messagebox.showinfo("保存完了", f"設定を保存しました\n{config_path}")


class EditDialog:
    """抽出データ編集ダイアログ"""

    def __init__(self, parent, invoice: InvoiceData, callback, idx: int, item_id: str):
        self.invoice = invoice
        self.callback = callback
        self.idx = idx
        self.item_id = item_id

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("振込データ編集")
        self.dialog.geometry("450x380")
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()

        frame = ttk.Frame(self.dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        self.vars = {}
        fields = [
            ("payee_name", "受取人名（口座名義）", invoice.payee_name),
            ("bank_name", "銀行名", invoice.bank_name or ""),
            ("bank_code", "銀行コード (4桁)", invoice.bank_code or ""),
            ("branch_name", "支店名", invoice.branch_name or ""),
            ("branch_code", "支店コード (3桁)", invoice.branch_code or ""),
            ("account_type", "預金種目 (普通/当座)", invoice.account_type),
            ("account_number", "口座番号", invoice.account_number),
            ("amount", "金額 (円)", str(invoice.amount)),
        ]

        for i, (key, label, value) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky=tk.W, pady=3)
            var = tk.StringVar(value=value)
            self.vars[key] = var
            entry = ttk.Entry(frame, textvariable=var, width=35)
            entry.grid(row=i, column=1, sticky=tk.W, padx=10, pady=3)
            if i == 0:
                entry.focus_set()

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=len(fields), column=0, columnspan=2, pady=15)

        ttk.Button(btn_frame, text="保存", command=self._save).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="キャンセル", command=self.dialog.destroy).pack(side=tk.LEFT, padx=10)

        self.dialog.bind("<Return>", lambda e: self._save())
        self.dialog.bind("<Escape>", lambda e: self.dialog.destroy())

    def _save(self):
        try:
            amount_str = self.vars["amount"].get().replace(",", "").replace("，", "")
            amount = int(amount_str)
        except ValueError:
            messagebox.showerror("エラー", "金額は数値で入力してください", parent=self.dialog)
            return

        updated = InvoiceData(
            payee_name=self.vars["payee_name"].get().strip(),
            bank_name=self.vars["bank_name"].get().strip() or None,
            bank_code=self.vars["bank_code"].get().strip() or None,
            branch_name=self.vars["branch_name"].get().strip() or None,
            branch_code=self.vars["branch_code"].get().strip() or None,
            account_type=self.vars["account_type"].get().strip(),
            account_number=self.vars["account_number"].get().strip(),
            amount=amount,
        )

        self.callback(self.idx, self.item_id, updated)
        self.dialog.destroy()


def main():
    root = tk.Tk()
    app = ZenginConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
