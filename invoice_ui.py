from __future__ import annotations

import base64
import getpass
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import fitz

from invoice_database import InvoiceDatabase
from invoice_engine import InvoiceEngine, find_pdfs, write_results_csv
from invoice_models import RESOURCE_POLICIES, STATUS_DUPLICATE, STATUS_FAILED, STATUS_OK, STATUS_REVIEW, InvoiceResult
from version import APP_NAME, APP_VERSION

APP_TITLE = f"{APP_NAME} v{APP_VERSION}"
BG = "#F5F5F5"; CARD = "#FFFFFF"; TEXT = "#1F1F1F"; MUTED = "#666666"; ACCENT = "#0067C0"
SUCCESS = "#0F7B0F"; WARNING = "#9D5D00"; ERROR = "#C42B1C"

FIELD_LABELS = {
    "account_number": "Account number", "bill_date": "Bill date", "billing_period": "Billing period",
    "current_charges": "Current charges", "total_amount_due": "Total amount due", "meter_number": "Meter number",
    "consumption": "Consumption", "consumption_unit": "Consumption unit", "previous_balance": "Previous balance",
    "payments": "Payments",
}


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(round(seconds))); minutes, sec = divmod(seconds, 60); hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}" if hours else f"{minutes:02d}:{sec:02d}"


def open_file(path: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        messagebox.showerror("Unable to open file", str(exc))


def db_path_for_output(output: str | Path) -> Path:
    return Path(output).with_name("invoice_intelligence_v4.db")


class InvoiceApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE); self.geometry("1380x860"); self.minsize(1100, 720); self.configure(bg=BG)
        self._set_windows_dpi_awareness()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.files: list[Path] = []; self.results: list[InvoiceResult] = []; self.processing = False
        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(Path.home() / "Documents" / "invoice_results_v4.csv"))
        self.recursive_var = tk.BooleanVar(value=False); self.force_var = tk.BooleanVar(value=False)
        self.resource_var = tk.StringVar(value="Balanced"); self.actor_var = tk.StringVar(value=getpass.getuser() or "Local user")
        self.progress_text = tk.StringVar(value="Ready"); self.current_file_var = tk.StringVar(value="Choose a folder to begin.")
        self.selected_count_var = tk.StringVar(value="0 selected")
        self.summary_vars = {k: tk.StringVar(value=v) for k, v in {
            "Files": "0", "Amount": "$0.00", "OK": "0", "Review": "0", "Duplicates": "0"
        }.items()}
        self._configure_style(); self._build_ui(); self.after(100, self._poll_events)

    @staticmethod
    def _set_windows_dpi_awareness() -> None:
        if not sys.platform.startswith("win"): return
        try:
            import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception: pass

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if sys.platform.startswith("win") and "vista" in style.theme_names(): style.theme_use("vista")
        style.configure("TFrame", background=BG); style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 22))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("CardValue.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI Semibold", 18))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(16, 8))
        style.configure("TButton", font=("Segoe UI", 10), padding=(11, 7)); style.configure("TCheckbutton", background=CARD)
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 9), background=CARD, fieldbackground=CARD, borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), padding=(6, 8))
        style.configure("Horizontal.TProgressbar", thickness=8)

    def _build_ui(self) -> None:
        container = ttk.Frame(self, padding=(24, 20, 24, 18)); container.pack(fill="both", expand=True)
        header = ttk.Frame(container); header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(side="left", anchor="w")
        header_actions = ttk.Frame(header); header_actions.pack(side="right")
        ttk.Button(header_actions, text="Portfolio master", command=self.open_portfolio_manager).pack(side="left")
        ttk.Button(header_actions, text="Review center", command=self.open_review_center).pack(side="left", padx=(6, 0))
        ttk.Button(header_actions, text="Run history", command=self.open_run_history).pack(side="left", padx=(6, 0))
        ttk.Button(header_actions, text="DB health / backup", command=self.database_health).pack(side="left", padx=(6, 0))
        ttk.Label(container, text="Offline portfolio-aware utility invoice assurance · validation · audit trail · resumable processing",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))

        controls = ttk.Frame(container, style="Card.TFrame", padding=16); controls.pack(fill="x", pady=(18, 12)); controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Invoice folder", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(controls, textvariable=self.source_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(controls, text="Browse…", command=self.choose_source).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(controls, text="Scan folder", command=self.scan_folder).grid(row=0, column=3, padx=(8, 0))
        ttk.Label(controls, text="Results CSV", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(10, 0))
        ttk.Entry(controls, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", pady=(10, 0))
        ttk.Button(controls, text="Save as…", command=self.choose_output).grid(row=1, column=2, padx=(8, 0), pady=(10, 0))
        options = ttk.Frame(controls, style="Card.TFrame"); options.grid(row=2, column=1, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Checkbutton(options, text="Include subfolders", variable=self.recursive_var).pack(side="left")
        ttk.Checkbutton(options, text="Reprocess unchanged files", variable=self.force_var).pack(side="left", padx=(16, 0))
        ttk.Label(options, text="Performance", style="Card.TLabel").pack(side="left", padx=(20, 6))
        ttk.Combobox(options, textvariable=self.resource_var, values=list(RESOURCE_POLICIES), state="readonly", width=17).pack(side="left")
        ttk.Label(options, text="Reviewer", style="Card.TLabel").pack(side="left", padx=(20, 6))
        ttk.Entry(options, textvariable=self.actor_var, width=18).pack(side="left")

        summary = ttk.Frame(container); summary.pack(fill="x", pady=(0, 12))
        for i, (label, variable) in enumerate(self.summary_vars.items()):
            summary.columnconfigure(i, weight=1); card = ttk.Frame(summary, style="Card.TFrame", padding=(14, 10))
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 5, 0 if i == 4 else 5))
            ttk.Label(card, text=label, style="CardTitle.TLabel").pack(anchor="w"); ttk.Label(card, textvariable=variable, style="CardValue.TLabel").pack(anchor="w", pady=(2, 0))

        body = ttk.Panedwindow(container, orient="horizontal"); body.pack(fill="both", expand=True)
        left = ttk.Frame(body, style="Card.TFrame", padding=12); right = ttk.Frame(body, style="Card.TFrame", padding=12)
        body.add(left, weight=2); body.add(right, weight=3)
        file_header = ttk.Frame(left, style="Card.TFrame"); file_header.pack(fill="x", pady=(0, 8))
        ttk.Label(file_header, text="Invoices", style="Card.TLabel", font=("Segoe UI Semibold", 11)).pack(side="left")
        ttk.Label(file_header, textvariable=self.selected_count_var, style="CardTitle.TLabel").pack(side="left", padx=(8, 0))
        ttk.Button(file_header, text="All", command=self.select_all_files).pack(side="right")
        ttk.Button(file_header, text="None", command=self.clear_file_selection).pack(side="right", padx=(0, 6))
        self.file_tree = ttk.Treeview(left, columns=("name","size"), show="headings", selectmode="extended")
        self.file_tree.heading("name", text="File"); self.file_tree.heading("size", text="Size")
        self.file_tree.column("name", width=340); self.file_tree.column("size", width=90, anchor="e")
        fs = ttk.Scrollbar(left, orient="vertical", command=self.file_tree.yview); self.file_tree.configure(yscrollcommand=fs.set)
        self.file_tree.pack(side="left", fill="both", expand=True); fs.pack(side="right", fill="y")
        self.file_tree.bind("<<TreeviewSelect>>", lambda _: self._update_selected_count())

        result_header = ttk.Frame(right, style="Card.TFrame"); result_header.pack(fill="x", pady=(0, 8))
        ttk.Label(result_header, text="Analysis results", style="Card.TLabel", font=("Segoe UI Semibold", 11)).pack(side="left")
        ttk.Button(result_header, text="Open PDF", command=self.open_selected_result).pack(side="right")
        ttk.Button(result_header, text="Map account…", command=self.map_selected_account).pack(side="right", padx=(0, 6))
        columns = ("property","vendor","account","date","total","meter","confidence","status")
        self.result_tree = ttk.Treeview(right, columns=columns, show="headings", selectmode="browse", height=10)
        labels = {"property":"Project","vendor":"Vendor","account":"Account","date":"Bill date","total":"Total due","meter":"Meter","confidence":"Confidence","status":"Status"}
        widths = {"property":80,"vendor":100,"account":130,"date":95,"total":100,"meter":100,"confidence":85,"status":130}
        for key in columns:
            self.result_tree.heading(key, text=labels[key]); self.result_tree.column(key, width=widths[key], anchor="e" if key in {"total","confidence"} else "w")
        self.result_tree.tag_configure("ok", foreground=SUCCESS); self.result_tree.tag_configure("review", foreground=WARNING)
        self.result_tree.tag_configure("duplicate", foreground=ACCENT); self.result_tree.tag_configure("failed", foreground=ERROR)
        self.result_tree.pack(fill="both", expand=True); self.result_tree.bind("<<TreeviewSelect>>", self._show_result_details)
        ttk.Label(right, text="Highlights / review notes", style="Card.TLabel", font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(10, 0))
        self.details = tk.Text(right, height=7, wrap="word", bd=0, bg="#FAFAFA", fg=TEXT, font=("Segoe UI", 9), padx=10, pady=8, state="disabled")
        self.details.pack(fill="x", pady=(6, 0))

        footer = ttk.Frame(container); footer.pack(fill="x", pady=(12, 0))
        self.progress = ttk.Progressbar(footer, orient="horizontal", mode="determinate", maximum=100); self.progress.pack(fill="x")
        row = ttk.Frame(footer); row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, textvariable=self.current_file_var, style="Subtitle.TLabel").pack(side="left")
        ttk.Label(row, textvariable=self.progress_text, style="Subtitle.TLabel").pack(side="right")
        actions = ttk.Frame(container); actions.pack(fill="x", pady=(12, 0))
        self.cancel_btn = ttk.Button(actions, text="Cancel safely", command=self.cancel_analysis); self.cancel_btn.pack(side="left"); self.cancel_btn.state(["disabled"])
        self.analyze_selected_btn = ttk.Button(actions, text="Analyze selected", style="Accent.TButton", command=self.analyze_selected); self.analyze_selected_btn.pack(side="right")
        self.analyze_all_btn = ttk.Button(actions, text="Analyze all", command=self.analyze_all); self.analyze_all_btn.pack(side="right", padx=(0, 8))

    def choose_source(self) -> None:
        path = filedialog.askdirectory(title="Choose invoice folder")
        if path:
            self.source_var.set(path)
            if self.output_var.get().endswith(("invoice_results.csv", "invoice_results_v4.csv")):
                self.output_var.set(str(Path(path) / "invoice_results_v4.csv"))
            self.scan_folder()

    def choose_output(self) -> None:
        path = filedialog.asksaveasfilename(title="Save analysis results", defaultextension=".csv", filetypes=[("CSV file","*.csv")], initialfile="invoice_results_v4.csv")
        if path: self.output_var.set(path)

    def scan_folder(self) -> None:
        source = self.source_var.get().strip()
        if not source: messagebox.showinfo(APP_TITLE, "Choose an invoice folder first."); return
        base = Path(source)
        if not base.is_dir(): messagebox.showerror(APP_TITLE, "The selected invoice folder does not exist."); return
        self.files = find_pdfs(base, recursive=self.recursive_var.get()); self.file_tree.delete(*self.file_tree.get_children())
        for index, path in enumerate(self.files):
            self.file_tree.insert("", "end", iid=str(index), values=(path.name, f"{path.stat().st_size/(1024*1024):.2f} MB"))
        self.select_all_files(); self.current_file_var.set(f"Found {len(self.files)} PDF invoice(s)."); self.progress_text.set("Ready"); self.progress["value"] = 0
        self.summary_vars["Files"].set(str(len(self.files)))

    def select_all_files(self) -> None:
        self.file_tree.selection_set(self.file_tree.get_children()); self._update_selected_count()

    def clear_file_selection(self) -> None:
        self.file_tree.selection_remove(self.file_tree.selection()); self._update_selected_count()

    def _update_selected_count(self) -> None: self.selected_count_var.set(f"{len(self.file_tree.selection())} selected")

    def _selected_paths(self) -> list[Path]:
        out=[]
        for iid in self.file_tree.selection():
            try: out.append(self.files[int(iid)])
            except (ValueError, IndexError): pass
        return out

    def analyze_selected(self) -> None: self._start_analysis(self._selected_paths())
    def analyze_all(self) -> None: self._start_analysis(self.files)

    def _start_analysis(self, paths: list[Path], resume_run_id: str | None = None) -> None:
        if self.processing: return
        if not paths and not resume_run_id: messagebox.showinfo(APP_TITLE, "Select at least one invoice."); return
        output = self.output_var.get().strip()
        if not output: messagebox.showinfo(APP_TITLE, "Choose a results CSV location."); return
        output_path = Path(output)
        if output_path.suffix.lower() != ".csv": output_path = output_path.with_suffix(".csv"); self.output_var.set(str(output_path))
        try: output_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc: messagebox.showerror(APP_TITLE, f"Unable to create the output folder:\n{exc}"); return
        self.processing = True; self.cancel_event.clear(); self.results = []; self.result_tree.delete(*self.result_tree.get_children())
        self._set_details("Processing invoices…"); self.progress["value"] = 0
        self.progress_text.set(f"0 / {len(paths)} · estimating time" if paths else "Resuming run…")
        self.current_file_var.set("Starting analysis…"); self._set_processing_buttons(True)
        thread = threading.Thread(target=self._analysis_worker, args=(paths, output_path, bool(self.force_var.get()), resume_run_id), daemon=True); thread.start()

    def _set_processing_buttons(self, active: bool) -> None:
        if active:
            self.analyze_selected_btn.state(["disabled"]); self.analyze_all_btn.state(["disabled"]); self.cancel_btn.state(["!disabled"])
        else:
            self.analyze_selected_btn.state(["!disabled"]); self.analyze_all_btn.state(["!disabled"]); self.cancel_btn.state(["disabled"])

    def cancel_analysis(self) -> None:
        if self.processing:
            self.cancel_event.set(); self.current_file_var.set("Cancellation requested — finishing the current PDF safely…")

    def _analysis_worker(self, paths: list[Path], output_path: Path, force: bool, resume_run_id: str | None) -> None:
        engine = None
        try:
            engine = InvoiceEngine(str(db_path_for_output(output_path)), resource_mode=self.resource_var.get(), auto_seed_portfolio=True)
            results = engine.process_files(
                paths, force=force, progress=lambda payload: self.events.put(("progress", payload)),
                cancel_requested=self.cancel_event.is_set, run_id=resume_run_id,
                source_label=self.source_var.get(), output_path=str(output_path),
            )
            engine.export_csv(results, str(output_path))
            self.events.put(("done", {"results":results,"csv":str(output_path),"db":str(db_path_for_output(output_path)),"cancelled":self.cancel_event.is_set()}))
        except Exception as exc: self.events.put(("error", str(exc)))
        finally:
            if engine: engine.close()

    def _poll_events(self) -> None:
        try:
            while True:
                event,payload = self.events.get_nowait()
                if event == "progress": self._handle_progress(payload)  # type: ignore[arg-type]
                elif event == "done": self._handle_done(payload)  # type: ignore[arg-type]
                elif event == "error": self._handle_error(str(payload))
        except queue.Empty: pass
        self.after(100, self._poll_events)

    def _handle_progress(self, payload: dict) -> None:
        self.progress["value"] = float(payload.get("fraction",0))*100
        index,total,eta = int(payload.get("index",0)),int(payload.get("total",0)),float(payload.get("eta_seconds",0))
        self.current_file_var.set(("Cached: " if payload.get("skipped") else "Analyzed: ") + str(payload.get("current_file","")))
        self.progress_text.set(f"{index} / {total} · ETA {format_seconds(eta)}")
        result=payload.get("result")
        if isinstance(result, InvoiceResult): self.results.append(result); self._insert_result(result); self._update_summary(self.results)

    def _handle_done(self, payload: dict) -> None:
        self.processing=False; self._set_processing_buttons(False)
        if payload.get("cancelled"):
            self.progress_text.set(f"Cancelled safely · {len(payload['results'])} completed/cached"); self.current_file_var.set("Run state saved; use Run history to resume.")
        else:
            self.progress["value"]=100; self.progress_text.set(f"Complete · {len(payload['results'])} invoice(s)"); self.current_file_var.set(f"Saved CSV: {payload['csv']}")
        self._update_summary(payload["results"])

    def _handle_error(self, message: str) -> None:
        self.processing=False; self._set_processing_buttons(False); self.progress_text.set("Stopped"); messagebox.showerror(APP_TITLE,message)

    def _insert_result(self, item: InvoiceResult) -> None:
        amount=f"${item.total_amount_due:,.2f}" if item.total_amount_due is not None else "—"; confidence=f"{item.confidence:.0%}" if item.confidence else "—"
        tag="ok" if item.status==STATUS_OK else "duplicate" if item.status==STATUS_DUPLICATE else "failed" if item.status==STATUS_FAILED else "review"
        account=item.account_number_raw or item.account_number or "—"
        self.result_tree.insert("","end",iid=str(len(self.result_tree.get_children())),values=(item.property_id or item.suggested_property_id or "—",item.vendor,account,item.bill_date or "—",amount,item.meter_number or "—",confidence,item.status),tags=(tag,))

    def _update_summary(self, results: list[InvoiceResult]) -> None:
        self.summary_vars["Files"].set(str(len(results)))
        total=sum(float(i.total_amount_due) for i in results if i.total_amount_due is not None and i.status!=STATUS_DUPLICATE)
        self.summary_vars["Amount"].set(f"${total:,.2f}"); self.summary_vars["OK"].set(str(sum(i.status==STATUS_OK for i in results)))
        self.summary_vars["Review"].set(str(sum(i.status in {STATUS_REVIEW,STATUS_FAILED} for i in results))); self.summary_vars["Duplicates"].set(str(sum(i.status==STATUS_DUPLICATE for i in results)))

    def _show_result_details(self,_event=None) -> None:
        sel=self.result_tree.selection()
        if not sel:return
        try:item=self.results[int(sel[0])]
        except (ValueError,IndexError):return
        lines=[item.file_name,f"Status: {item.status}    Confidence: {item.confidence:.1%}",f"Document ID: {item.document_id or '—'}"]
        if item.property_id: lines.append(f"Project: {item.property_id} — {item.property_name or ''}")
        elif item.suggested_property_id: lines.append(f"Suggested project: {item.suggested_property_id} (not yet mapped)")
        if item.account_number_raw:
            lines.append(f"Account (PDF): {item.account_number_raw}")
            if item.account_number_raw.replace(" ","") != (item.account_number or ""): lines.append(f"Canonical account: {item.account_number}")
            elif item.account_number != item.account_number_raw: lines.append(f"Canonical account: {item.account_number}")
        elif item.account_number: lines.append(f"Account: {item.account_number}")
        if item.total_amount_due is not None: lines.append(f"Total due: ${item.total_amount_due:,.2f}")
        if item.meter_number: lines.append(f"Meter: {item.meter_number}")
        if item.billing_period_start and item.billing_period_end: lines.append(f"Billing period: {item.billing_period_start} → {item.billing_period_end}")
        if item.consumption is not None: lines.append(f"Consumption: {item.consumption:,.2f} {item.consumption_unit or ''}".rstrip())
        if item.effective_unit_cost is not None: lines.append(f"Effective unit cost: {item.effective_unit_cost:.6f} per {item.consumption_unit or 'unit'}")
        if item.portfolio_flags: lines.append("Portfolio controls: " + ", ".join(item.portfolio_flags))
        lines.append(f"Parser {item.parser_version or '—'} · Rules {item.rule_version or '—'} · App {item.app_version or '—'}")
        if item.duplicate_of: lines.append(f"Duplicate of: {item.duplicate_of}")
        if item.near_duplicate_of: lines.append(f"Possible revised invoice: {item.near_duplicate_of}")
        if item.issues: lines.append("\nReview required:"); lines.extend(f"• {m}" for m in item.issues)
        if item.warnings: lines.append("\nWarnings:"); lines.extend(f"• {m}" for m in item.warnings)
        if not item.issues and not item.warnings: lines.append("\nNo validation exceptions detected.")
        self._set_details("\n".join(lines))

    def _set_details(self,text:str)->None:
        self.details.configure(state="normal"); self.details.delete("1.0","end"); self.details.insert("1.0",text); self.details.configure(state="disabled")

    def open_selected_result(self)->None:
        sel=self.result_tree.selection()
        if not sel: messagebox.showinfo(APP_TITLE,"Select a result first."); return
        try: open_file(self.results[int(sel[0])].file_path)
        except (ValueError,IndexError): pass

    def _current_db_path(self) -> Path:
        return db_path_for_output(self.output_var.get())

    def open_portfolio_manager(self) -> None:
        path = self._current_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        PortfolioManager(self, path, self.actor_var.get())

    def map_selected_account(self) -> None:
        sel = self.result_tree.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select an analyzed invoice first.")
            return
        try:
            item = self.results[int(sel[0])]
        except (ValueError, IndexError):
            return
        if not item.account_number or item.vendor == "UNKNOWN":
            messagebox.showinfo(APP_TITLE, "This invoice does not have a usable vendor/account identity.")
            return
        db_path = self._current_db_path()
        if not db_path.exists():
            messagebox.showinfo(APP_TITLE, "Analyze the invoice first so the v4 database exists.")
            return
        def chosen(project_id: str) -> None:
            try:
                with InvoiceDatabase(str(db_path), create_backup=False) as db:
                    db.map_utility_account(item.vendor, item.account_number, project_id,
                                           display_account=item.account_number_raw or item.account_number,
                                           meter_number=item.meter_number, actor=self.actor_var.get())
                    updated = db.get_result_by_document(item.document_id or "")
                if updated:
                    idx = int(sel[0]); self.results[idx] = updated
                    amount=f"${updated.total_amount_due:,.2f}" if updated.total_amount_due is not None else "—"
                    confidence=f"{updated.confidence:.0%}" if updated.confidence else "—"
                    account=updated.account_number_raw or updated.account_number or "—"
                    tag="ok" if updated.status==STATUS_OK else "duplicate" if updated.status==STATUS_DUPLICATE else "failed" if updated.status==STATUS_FAILED else "review"
                    self.result_tree.item(sel[0], values=(updated.property_id or "—",updated.vendor,account,updated.bill_date or "—",amount,updated.meter_number or "—",confidence,updated.status), tags=(tag,))
                    self._update_summary(self.results)
                    self._show_result_details()
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc))
        PropertyPicker(self, db_path, chosen)

    def database_health(self) -> None:
        path=self._current_db_path()
        if not path.exists(): messagebox.showinfo(APP_TITLE,"No v4 database exists beside the selected CSV yet."); return
        try:
            with InvoiceDatabase(str(path), create_backup=False) as db:
                ok,msg=db.integrity_check(quick=True)
                if not ok: messagebox.showerror(APP_TITLE,f"Database check failed: {msg}"); return
                backup=db.backup(reason="manual")
            messagebox.showinfo(APP_TITLE,f"SQLite quick_check: OK\nBackup created:\n{backup}")
        except Exception as exc: messagebox.showerror(APP_TITLE,str(exc))

    # ---------------- field-level review center ----------------
    def open_review_center(self) -> None:
        path=self._current_db_path()
        if not path.exists(): messagebox.showinfo(APP_TITLE,"Analyze invoices first, or select a CSV location beside an existing v4 database."); return
        ReviewCenter(self, path, self.actor_var.get())

    # ---------------- run history / resume ----------------
    def open_run_history(self) -> None:
        path=self._current_db_path()
        if not path.exists(): messagebox.showinfo(APP_TITLE,"No run history exists yet."); return
        win=tk.Toplevel(self); win.title("Run history"); win.geometry("900x480"); win.configure(bg=BG)
        frame=ttk.Frame(win,padding=16); frame.pack(fill="both",expand=True)
        tree=ttk.Treeview(frame,columns=("run","status","progress","started","ended","source"),show="headings")
        for key,label,width in (("run","Run ID",210),("status","Status",110),("progress","Progress",90),("started","Started",145),("ended","Ended",145),("source","Source",180)):
            tree.heading(key,text=label); tree.column(key,width=width)
        tree.pack(fill="both",expand=True)
        db=InvoiceDatabase(str(path),create_backup=False)
        rows=db.recent_runs(); lookup={}
        for i,row in enumerate(rows):
            iid=str(i); lookup[iid]=row; tree.insert("","end",iid=iid,values=(row["run_id"],row["status"],f"{row['completed_items']}/{row['total_items']}",row["started_at"],row["ended_at"] or "—",row["source_label"] or ""))
        def resume():
            sel=tree.selection()
            if not sel:return
            row=lookup[sel[0]]
            if row["status"] not in {"INTERRUPTED","CANCELLED","FAILED"}: messagebox.showinfo(APP_TITLE,"Only interrupted, cancelled or failed runs need resuming."); return
            output=row["output_path"] or self.output_var.get(); self.output_var.set(output)
            run_id=row["run_id"]; db.close(); win.destroy(); self._start_analysis([],resume_run_id=run_id)
        buttons=ttk.Frame(frame); buttons.pack(fill="x",pady=(10,0)); ttk.Button(buttons,text="Resume selected run",style="Accent.TButton",command=resume).pack(side="right")
        win.protocol("WM_DELETE_WINDOW",lambda:(db.close(),win.destroy()))


class PropertyPicker(tk.Toplevel):
    def __init__(self, parent: tk.Misc, db_path: Path, callback):
        super().__init__(parent)
        self.db_path = Path(db_path); self.callback = callback
        self.title("Choose canonical internal project"); self.geometry("820x600"); self.minsize(680, 450)
        self.transient(parent); self.grab_set()
        frame = ttk.Frame(self, padding=16); frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Map utility account to project", font=("Segoe UI Semibold", 15)).pack(anchor="w")
        ttk.Label(frame, text="Aliases such as 5093-1 / 5093-2 / 5093-3 resolve to canonical internal project 5093.", style="Subtitle.TLabel").pack(anchor="w", pady=(3,10))
        row = ttk.Frame(frame); row.pack(fill="x")
        self.query = tk.StringVar(); entry = ttk.Entry(row, textvariable=self.query); entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Search", command=self.refresh).pack(side="left", padx=(8,0)); entry.bind("<Return>", lambda _e:self.refresh())
        self.tree = ttk.Treeview(frame, columns=("project","name","strata","pm"), show="headings", selectmode="browse")
        for key,label,width in (("project","Project",90),("name","Property",400),("strata","Strata",110),("pm","PM",90)):
            self.tree.heading(key,text=label); self.tree.column(key,width=width)
        self.tree.pack(fill="both", expand=True, pady=(10,10)); self.tree.bind("<Double-1>", lambda _e:self.choose())
        actions=ttk.Frame(frame); actions.pack(fill="x"); ttk.Button(actions,text="Cancel",command=self.destroy).pack(side="right")
        ttk.Button(actions,text="Use selected project",style="Accent.TButton",command=self.choose).pack(side="right",padx=(0,8))
        self.refresh(); entry.focus_set()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        with InvoiceDatabase(str(self.db_path), create_backup=False) as db:
            rows=db.list_properties(self.query.get(), limit=2500)
        for row in rows:
            self.tree.insert("","end",iid=str(row["project_id"]),values=(row["project_id"],row["project_name"],row["strata_plan"] or "—",row["pm"] or "—"))

    def choose(self):
        sel=self.tree.selection()
        if not sel: messagebox.showinfo(APP_TITLE,"Select a project first.",parent=self); return
        project_id=str(sel[0]); self.grab_release(); self.destroy(); self.callback(project_id)


class PortfolioManager(tk.Toplevel):
    def __init__(self, parent: tk.Misc, db_path: Path, actor: str):
        super().__init__(parent)
        self.db_path=Path(db_path); self.actor=actor
        self.title("Portfolio master · v4"); self.geometry("1120x760"); self.minsize(900,620)
        self._ensure_seed(); self._build(); self.refresh()

    def _ensure_seed(self):
        with InvoiceDatabase(str(self.db_path), create_backup=False) as db:
            if not db.has_portfolio():
                bundled=Path(__file__).resolve().parent/"data"/"pm.csv"
                if bundled.exists(): db.import_property_master(bundled)

    def _build(self):
        frame=ttk.Frame(self,padding=18); frame.pack(fill="both",expand=True)
        header=ttk.Frame(frame); header.pack(fill="x")
        ttk.Label(header,text="Property × Vendor × Account × Meter Master",font=("Segoe UI Semibold",17)).pack(side="left")
        ttk.Button(header,text="Import / update PM CSV…",command=self.import_csv).pack(side="right")
        ttk.Label(frame,text="Source-aware project normalization: aliases in one PROJ # cell map to its first explicit internal project. Example: 5093-1/2/3 → 5093; a standalone 5164-10 is not guessed as 5164.",style="Subtitle.TLabel",wraplength=1000).pack(anchor="w",pady=(4,12))
        cards=ttk.Frame(frame); cards.pack(fill="x",pady=(0,12))
        self.count_vars={k:tk.StringVar(value="0") for k in ("Properties","Utility accounts","Meters","Unmapped invoices")}
        for i,(k,v) in enumerate(self.count_vars.items()):
            cards.columnconfigure(i,weight=1); c=ttk.Frame(cards,style="Card.TFrame",padding=(12,8)); c.grid(row=0,column=i,sticky="ew",padx=(0 if i==0 else 4,0 if i==3 else 4))
            ttk.Label(c,text=k,style="CardTitle.TLabel").pack(anchor="w"); ttk.Label(c,textvariable=v,style="CardValue.TLabel").pack(anchor="w")
        notebook=ttk.Notebook(frame); notebook.pack(fill="both",expand=True)
        prop=ttk.Frame(notebook,padding=10); util=ttk.Frame(notebook,padding=10); notebook.add(prop,text="Properties"); notebook.add(util,text="Utility master")
        searchrow=ttk.Frame(prop); searchrow.pack(fill="x",pady=(0,8)); self.query=tk.StringVar(); ttk.Entry(searchrow,textvariable=self.query).pack(side="left",fill="x",expand=True)
        ttk.Button(searchrow,text="Search",command=self.refresh_properties).pack(side="left",padx=(8,0))
        self.prop_tree=ttk.Treeview(prop,columns=("project","name","strata","pm","source"),show="headings")
        for k,l,w in (("project","Project",85),("name","Property",340),("strata","Strata",100),("pm","PM",90),("source","Source PROJ #",260)):
            self.prop_tree.heading(k,text=l); self.prop_tree.column(k,width=w)
        self.prop_tree.pack(fill="both",expand=True)
        self.utility_tree=ttk.Treeview(util,columns=("project","property","vendor","account","meters"),show="headings")
        for k,l,w in (("project","Project",85),("property","Property",300),("vendor","Vendor",110),("account","Canonical account",180),("meters","Meters",80)):
            self.utility_tree.heading(k,text=l); self.utility_tree.column(k,width=w)
        self.utility_tree.pack(fill="both",expand=True)

    def import_csv(self):
        path=filedialog.askopenfilename(parent=self,title="Import portfolio CSV",filetypes=[("CSV file","*.csv"),("All files","*.*")])
        if not path:return
        try:
            with InvoiceDatabase(str(self.db_path),create_backup=False) as db: stats=db.import_property_master(path)
            messagebox.showinfo(APP_TITLE,f"Portfolio updated.\nProperties: {stats['properties']}\nAliases: {stats['aliases']}\nPostal codes: {stats['postal_codes']}",parent=self); self.refresh()
        except Exception as exc:messagebox.showerror(APP_TITLE,str(exc),parent=self)

    def refresh(self):
        with InvoiceDatabase(str(self.db_path),create_backup=False) as db: counts=db.portfolio_counts()
        self.count_vars["Properties"].set(str(counts["properties"])); self.count_vars["Utility accounts"].set(str(counts["utility_accounts"])); self.count_vars["Meters"].set(str(counts["meters"])); self.count_vars["Unmapped invoices"].set(str(counts["unmapped_invoices"]))
        self.refresh_properties(); self.refresh_utilities()

    def refresh_properties(self):
        self.prop_tree.delete(*self.prop_tree.get_children())
        with InvoiceDatabase(str(self.db_path),create_backup=False) as db: rows=db.list_properties(self.query.get(),limit=2500)
        for row in rows:self.prop_tree.insert("","end",values=(row["project_id"],row["project_name"],row["strata_plan"] or "—",row["pm"] or "—",(row["source_project_raw"] or "").replace("\r"," / ").replace("\n"," / ")))

    def refresh_utilities(self):
        self.utility_tree.delete(*self.utility_tree.get_children())
        with InvoiceDatabase(str(self.db_path),create_backup=False) as db: rows=db.list_utility_accounts()
        for row in rows:self.utility_tree.insert("","end",values=(row["property_id"],row["project_name"],row["vendor"],row["account_number_canonical"],row["meter_count"]))


class ReviewCenter(tk.Toplevel):
    def __init__(self, parent: InvoiceApp, db_path: Path, actor: str):
        super().__init__(parent); self.title("Field-level Review Center"); self.geometry("1450x860"); self.minsize(1150,700); self.configure(bg=BG)
        self.db=InvoiceDatabase(str(db_path),create_backup=False); self.actor_var=tk.StringVar(value=actor or "Local user")
        self.results: list[InvoiceResult]=[]; self.current: InvoiceResult|None=None; self.photo=None; self.zoom=1.15
        self._build(); self.refresh(); self.protocol("WM_DELETE_WINDOW",self._close)

    def _close(self): self.db.close(); self.destroy()

    def _build(self):
        root=ttk.Frame(self,padding=16); root.pack(fill="both",expand=True)
        top=ttk.Frame(root); top.pack(fill="x")
        ttk.Label(top,text="Review Center",style="Title.TLabel").pack(side="left")
        ttk.Label(top,text="Reviewer",style="Subtitle.TLabel").pack(side="left",padx=(24,6)); ttk.Entry(top,textvariable=self.actor_var,width=18).pack(side="left")
        ttk.Button(top,text="Refresh",command=self.refresh).pack(side="right")
        ttk.Button(top,text="Export reviewed data…",command=self.export_reviewed).pack(side="right",padx=(0,6))
        ttk.Button(top,text="Audit trail",command=self.show_audit).pack(side="right",padx=(0,6))
        pane=ttk.Panedwindow(root,orient="horizontal"); pane.pack(fill="both",expand=True,pady=(14,0))
        left=ttk.Frame(pane,style="Card.TFrame",padding=10); mid=ttk.Frame(pane,style="Card.TFrame",padding=10); right=ttk.Frame(pane,style="Card.TFrame",padding=10)
        pane.add(left,weight=2); pane.add(mid,weight=2); pane.add(right,weight=3)
        ttk.Label(left,text="Invoices needing review",style="Card.TLabel",font=("Segoe UI Semibold",10)).pack(anchor="w",pady=(0,8))
        self.invoice_tree=ttk.Treeview(left,columns=("vendor","file","status"),show="headings",selectmode="browse")
        for k,l,w in (("vendor","Vendor",90),("file","File",260),("status","Status",125)):
            self.invoice_tree.heading(k,text=l); self.invoice_tree.column(k,width=w)
        self.invoice_tree.pack(fill="both",expand=True); self.invoice_tree.bind("<<TreeviewSelect>>",self._invoice_selected)
        ttk.Label(mid,text="Extracted fields",style="Card.TLabel",font=("Segoe UI Semibold",10)).pack(anchor="w",pady=(0,8))
        self.field_tree=ttk.Treeview(mid,columns=("field","value","confidence","review"),show="headings",selectmode="browse")
        for k,l,w in (("field","Field",125),("value","Current value",155),("confidence","Confidence",85),("review","Review",90)):
            self.field_tree.heading(k,text=l); self.field_tree.column(k,width=w)
        self.field_tree.pack(fill="both",expand=True); self.field_tree.bind("<<TreeviewSelect>>",self._field_selected)
        actions=ttk.Frame(mid,style="Card.TFrame"); actions.pack(fill="x",pady=(10,0))
        ttk.Button(actions,text="Accept field",command=self.accept_field).pack(side="left")
        ttk.Button(actions,text="Correct field…",command=self.correct_field).pack(side="left",padx=(6,0))
        ttk.Button(actions,text="Finalize review",style="Accent.TButton",command=self.finalize).pack(side="right")
        ttk.Label(right,text="Source evidence",style="Card.TLabel",font=("Segoe UI Semibold",10)).pack(anchor="w")
        canvas_frame=ttk.Frame(right,style="Card.TFrame"); canvas_frame.pack(fill="both",expand=True,pady=(8,0))
        self.canvas=tk.Canvas(canvas_frame,bg="#ECECEC",highlightthickness=0); ys=ttk.Scrollbar(canvas_frame,orient="vertical",command=self.canvas.yview); xs=ttk.Scrollbar(canvas_frame,orient="horizontal",command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ys.set,xscrollcommand=xs.set); self.canvas.grid(row=0,column=0,sticky="nsew"); ys.grid(row=0,column=1,sticky="ns"); xs.grid(row=1,column=0,sticky="ew")
        canvas_frame.rowconfigure(0,weight=1); canvas_frame.columnconfigure(0,weight=1)
        bottom=ttk.Frame(right,style="Card.TFrame"); bottom.pack(fill="x",pady=(8,0))
        ttk.Button(bottom,text="Open original PDF",command=self.open_pdf).pack(side="right")
        self.note=tk.Text(bottom,height=6,wrap="word",bd=0,bg="#FAFAFA",font=("Segoe UI",9)); self.note.pack(side="left",fill="x",expand=True)

    def refresh(self):
        self.results=self.db.review_results(); self.invoice_tree.delete(*self.invoice_tree.get_children())
        for i,r in enumerate(self.results): self.invoice_tree.insert("","end",iid=str(i),values=(r.vendor,r.file_name,r.status))
        self.field_tree.delete(*self.field_tree.get_children()); self.canvas.delete("all"); self.note.delete("1.0","end"); self.current=None

    def _invoice_selected(self,_=None):
        sel=self.invoice_tree.selection()
        if not sel:return
        self.current=self.results[int(sel[0])]; self.field_tree.delete(*self.field_tree.get_children())
        reviews=self.db.latest_field_reviews(self.current.document_id or "")
        for key,label in FIELD_LABELS.items():
            value=getattr(self.current,key,None); ev=self.current.evidence.get(key,{})
            if key=="account_number" and self.current.account_number_raw:
                display=f"{self.current.account_number_raw}  →  {self.current.account_number}" if self.current.account_number_raw!=self.current.account_number else self.current.account_number
            elif isinstance(value,float): display=f"{value:,.2f}"
            else: display="—" if value is None else str(value)
            conf=f"{float(ev.get('confidence',0)):.0%}" if ev else "—"; review=reviews.get(key); decision=review["decision"] if review else "—"
            self.field_tree.insert("","end",iid=key,values=(label,display,conf,decision))
        text=[self.current.file_name,f"Status: {self.current.status}",f"Document: {self.current.document_id}"]
        if self.current.issues:text += ["","Review reasons:"]+[f"• {x}" for x in self.current.issues]
        if self.current.warnings:text += ["","Warnings:"]+[f"• {x}" for x in self.current.warnings]
        self.note.delete("1.0","end"); self.note.insert("1.0","\n".join(text)); self._render_page(1,None)

    def _field_selected(self,_=None):
        if not self.current:return
        sel=self.field_tree.selection()
        if not sel:return
        field=sel[0]; ev=self.current.evidence.get(field,{})
        self._render_page(int(ev.get("page") or 1),ev)

    def _render_page(self,page_num:int,evidence:dict|None):
        if not self.current:return
        try:
            with fitz.open(self.current.file_path) as doc:
                page_index=max(0,min(len(doc)-1,page_num-1)); page=doc.load_page(page_index); pix=page.get_pixmap(matrix=fitz.Matrix(self.zoom,self.zoom),alpha=False)
                data=base64.b64encode(pix.tobytes("png")).decode("ascii"); self.photo=tk.PhotoImage(data=data)
                self.canvas.delete("all"); self.canvas.create_image(0,0,image=self.photo,anchor="nw"); self.canvas.configure(scrollregion=(0,0,pix.width,pix.height))
                if evidence and all(evidence.get(k) is not None for k in ("x0","y0","x1","y1")):
                    x0=float(evidence["x0"])*self.zoom; y0=float(evidence["y0"])*self.zoom; x1=float(evidence["x1"])*self.zoom; y1=float(evidence["y1"])*self.zoom
                    self.canvas.create_rectangle(x0-4,y0-3,x1+4,y1+3,outline="#C42B1C",width=3)
                    self.canvas.xview_moveto(max(0,(x0-80)/max(pix.width,1))); self.canvas.yview_moveto(max(0,(y0-80)/max(pix.height,1)))
        except Exception as exc:
            self.canvas.delete("all"); self.canvas.create_text(20,20,anchor="nw",text=f"Preview unavailable: {exc}")

    def _selected_field(self)->str|None:
        sel=self.field_tree.selection(); return sel[0] if sel else None

    def accept_field(self):
        if not self.current:return
        field=self._selected_field()
        if not field: messagebox.showinfo(APP_TITLE,"Select a field first."); return
        try:self.db.review_field(self.current.document_id or "",field,"ACCEPT",actor=self.actor_var.get()); self._reload_current()
        except Exception as exc:messagebox.showerror(APP_TITLE,str(exc))

    def correct_field(self):
        if not self.current:return
        field=self._selected_field()
        if not field: messagebox.showinfo(APP_TITLE,"Select a field first."); return
        current=getattr(self.current,field,None); value=simpledialog.askstring("Correct field",f"{FIELD_LABELS[field]}\nCurrent: {current}\n\nEnter corrected value:",parent=self)
        if value is None:return
        reason=simpledialog.askstring("Reason", "Optional reason for correction:",parent=self) or "Manual correction"
        try:self.db.review_field(self.current.document_id or "",field,"CORRECT",value,actor=self.actor_var.get(),reason=reason); self._reload_current()
        except Exception as exc:messagebox.showerror(APP_TITLE,str(exc))

    def finalize(self):
        if not self.current:return
        if not messagebox.askyesno(APP_TITLE,"Finalize this invoice after human review?\nThis action is recorded in the audit trail.",parent=self):return
        try:
            self.db.finalize_review(self.current.document_id or "",actor=self.actor_var.get(),reason="Human review completed")
            self.refresh()
        except Exception as exc:messagebox.showerror(APP_TITLE,str(exc))

    def _reload_current(self):
        if not self.current:return
        doc=self.db.get_result_by_document(self.current.document_id or "")
        if doc:self.current=doc
        # Rebuild only field list, retaining invoice context.
        fake_index=next((i for i,r in enumerate(self.results) if r.document_id==self.current.document_id),None)
        if fake_index is not None:self.results[fake_index]=self.current
        self.field_tree.delete(*self.field_tree.get_children()); reviews=self.db.latest_field_reviews(self.current.document_id or "")
        for key,label in FIELD_LABELS.items():
            value=getattr(self.current,key,None); ev=self.current.evidence.get(key,{})
            display=f"{value:,.2f}" if isinstance(value,float) else "—" if value is None else str(value)
            if key=="account_number" and self.current.account_number_raw and self.current.account_number_raw!=self.current.account_number: display=f"{self.current.account_number_raw}  →  {self.current.account_number}"
            review=reviews.get(key); self.field_tree.insert("","end",iid=key,values=(label,display,f"{float(ev.get('confidence',0)):.0%}" if ev else "—",review["decision"] if review else "—"))

    def export_reviewed(self):
        target=filedialog.asksaveasfilename(parent=self,title="Export current v4 database results",defaultextension=".csv",filetypes=[("CSV file","*.csv")],initialfile="invoice_results_reviewed.csv")
        if not target:return
        try:
            write_results_csv(self.db.all_results(),target)
            messagebox.showinfo(APP_TITLE,f"Exported reviewed/current values to:\n{target}",parent=self)
        except Exception as exc:messagebox.showerror(APP_TITLE,str(exc),parent=self)

    def show_audit(self):
        if not self.current: messagebox.showinfo(APP_TITLE,"Select an invoice first.",parent=self); return
        rows=self.db.audit_for_document(self.current.document_id or "")
        win=tk.Toplevel(self); win.title("Document audit trail"); win.geometry("1000x500")
        tree=ttk.Treeview(win,columns=("time","event","field","actor","reason"),show="headings")
        for k,l,w in (("time","Time",155),("event","Event",210),("field","Field",130),("actor","Actor",120),("reason","Reason",320)):
            tree.heading(k,text=l); tree.column(k,width=w)
        for i,row in enumerate(rows):tree.insert("","end",iid=str(i),values=(row["event_time"],row["event_type"],row["field_name"] or "—",row["actor"] or "—",row["reason"] or ""))
        tree.pack(fill="both",expand=True,padx=12,pady=12)

    def open_pdf(self):
        if self.current:open_file(self.current.file_path)


def main()->None:
    InvoiceApp().mainloop()

if __name__=="__main__":main()
