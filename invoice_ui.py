from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from invoice_engine import InvoiceEngine, find_pdfs
from invoice_models import STATUS_DUPLICATE, STATUS_FAILED, STATUS_OK, STATUS_REVIEW, InvoiceResult

APP_TITLE = "Local Invoice Intelligence"
BG = "#F5F5F5"
CARD = "#FFFFFF"
TEXT = "#1F1F1F"
MUTED = "#666666"
ACCENT = "#0067C0"
BORDER = "#E3E3E3"
SUCCESS = "#0F7B0F"
WARNING = "#9D5D00"
ERROR = "#C42B1C"


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


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


class InvoiceApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1320x820")
        self.minsize(1080, 700)
        self.configure(bg=BG)
        self._set_windows_dpi_awareness()

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.files: list[Path] = []
        self.results: list[InvoiceResult] = []
        self.processing = False

        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(Path.home() / "Documents" / "invoice_results.csv"))
        self.recursive_var = tk.BooleanVar(value=False)
        self.force_var = tk.BooleanVar(value=False)
        self.progress_text = tk.StringVar(value="Ready")
        self.current_file_var = tk.StringVar(value="Choose a folder to begin.")
        self.selected_count_var = tk.StringVar(value="0 selected")

        self.summary_vars = {
            "Files": tk.StringVar(value="0"),
            "Amount": tk.StringVar(value="$0.00"),
            "OK": tk.StringVar(value="0"),
            "Review": tk.StringVar(value="0"),
            "Duplicates": tk.StringVar(value="0"),
        }

        self._configure_style()
        self._build_ui()
        self.after(100, self._poll_events)

    @staticmethod
    def _set_windows_dpi_awareness() -> None:
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if sys.platform.startswith("win") and "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 22))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("CardValue.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI Semibold", 18))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(16, 8))
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 7))
        style.configure("TCheckbutton", background=BG, font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 9), background=CARD, fieldbackground=CARD, borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), padding=(6, 8))
        style.configure("Horizontal.TProgressbar", thickness=8)

    def _build_ui(self) -> None:
        container = ttk.Frame(self, padding=(24, 20, 24, 18))
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container)
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Offline PDF extraction, validation, duplicate detection and historical anomaly review",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        controls = ttk.Frame(container, style="Card.TFrame", padding=16)
        controls.pack(fill="x", pady=(18, 12))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Invoice folder", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(controls, textvariable=self.source_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(controls, text="Browse…", command=self.choose_source).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(controls, text="Scan folder", command=self.scan_folder).grid(row=0, column=3, padx=(8, 0))

        ttk.Label(controls, text="Results CSV", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(10, 0))
        ttk.Entry(controls, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", pady=(10, 0))
        ttk.Button(controls, text="Save as…", command=self.choose_output).grid(row=1, column=2, padx=(8, 0), pady=(10, 0))

        options = ttk.Frame(controls, style="Card.TFrame")
        options.grid(row=2, column=1, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Checkbutton(options, text="Include subfolders", variable=self.recursive_var).pack(side="left")
        ttk.Checkbutton(options, text="Reprocess unchanged files", variable=self.force_var).pack(side="left", padx=(18, 0))
        ttk.Label(options, text="SQLite history database is saved beside the CSV.", style="CardTitle.TLabel").pack(side="left", padx=(18, 0))

        summary = ttk.Frame(container)
        summary.pack(fill="x", pady=(0, 12))
        for i, (label, variable) in enumerate(self.summary_vars.items()):
            summary.columnconfigure(i, weight=1)
            card = ttk.Frame(summary, style="Card.TFrame", padding=(14, 10))
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 5, 0 if i == 4 else 5))
            ttk.Label(card, text=label, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=variable, style="CardValue.TLabel").pack(anchor="w", pady=(2, 0))

        body = ttk.Panedwindow(container, orient="horizontal")
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, style="Card.TFrame", padding=12)
        right = ttk.Frame(body, style="Card.TFrame", padding=12)
        body.add(left, weight=2)
        body.add(right, weight=3)

        # Files pane
        file_header = ttk.Frame(left, style="Card.TFrame")
        file_header.pack(fill="x", pady=(0, 8))
        ttk.Label(file_header, text="Invoices", style="Card.TLabel", font=("Segoe UI Semibold", 11)).pack(side="left")
        ttk.Label(file_header, textvariable=self.selected_count_var, style="CardTitle.TLabel").pack(side="left", padx=(8, 0))
        ttk.Button(file_header, text="All", command=self.select_all_files).pack(side="right")
        ttk.Button(file_header, text="None", command=self.clear_file_selection).pack(side="right", padx=(0, 6))

        file_columns = ("name", "size")
        self.file_tree = ttk.Treeview(left, columns=file_columns, show="headings", selectmode="extended")
        self.file_tree.heading("name", text="File")
        self.file_tree.heading("size", text="Size")
        self.file_tree.column("name", width=330, anchor="w")
        self.file_tree.column("size", width=85, anchor="e")
        file_scroll = ttk.Scrollbar(left, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=file_scroll.set)
        self.file_tree.pack(side="left", fill="both", expand=True)
        file_scroll.pack(side="right", fill="y")
        self.file_tree.bind("<<TreeviewSelect>>", lambda _: self._update_selected_count())

        # Results pane
        result_header = ttk.Frame(right, style="Card.TFrame")
        result_header.pack(fill="x", pady=(0, 8))
        ttk.Label(result_header, text="Analysis results", style="Card.TLabel", font=("Segoe UI Semibold", 11)).pack(side="left")
        ttk.Button(result_header, text="Open PDF", command=self.open_selected_result).pack(side="right")

        result_columns = ("vendor", "account", "date", "total", "meter", "confidence", "status")
        self.result_tree = ttk.Treeview(right, columns=result_columns, show="headings", selectmode="browse", height=10)
        headings = {
            "vendor": "Vendor", "account": "Account", "date": "Bill date", "total": "Total due",
            "meter": "Meter", "confidence": "Confidence", "status": "Status",
        }
        widths = {"vendor": 100, "account": 125, "date": 95, "total": 100, "meter": 100, "confidence": 85, "status": 125}
        for key in result_columns:
            self.result_tree.heading(key, text=headings[key])
            self.result_tree.column(key, width=widths[key], anchor="e" if key in {"total", "confidence"} else "w")
        self.result_tree.tag_configure("ok", foreground=SUCCESS)
        self.result_tree.tag_configure("review", foreground=WARNING)
        self.result_tree.tag_configure("duplicate", foreground=ACCENT)
        self.result_tree.tag_configure("failed", foreground=ERROR)
        self.result_tree.pack(fill="both", expand=True)
        self.result_tree.bind("<<TreeviewSelect>>", self._show_result_details)

        detail_frame = ttk.Frame(right, style="Card.TFrame")
        detail_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(detail_frame, text="Highlights / review notes", style="Card.TLabel", font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.details = tk.Text(
            detail_frame,
            height=7,
            wrap="word",
            bd=0,
            relief="flat",
            bg="#FAFAFA",
            fg=TEXT,
            font=("Segoe UI", 9),
            padx=10,
            pady=8,
            state="disabled",
        )
        self.details.pack(fill="x", pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill="x", pady=(12, 0))
        self.progress = ttk.Progressbar(footer, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        status_row = ttk.Frame(footer)
        status_row.pack(fill="x", pady=(6, 0))
        ttk.Label(status_row, textvariable=self.current_file_var, style="Subtitle.TLabel").pack(side="left")
        ttk.Label(status_row, textvariable=self.progress_text, style="Subtitle.TLabel").pack(side="right")

        action_row = ttk.Frame(container)
        action_row.pack(fill="x", pady=(12, 0))
        self.analyze_selected_btn = ttk.Button(action_row, text="Analyze selected", style="Accent.TButton", command=self.analyze_selected)
        self.analyze_selected_btn.pack(side="right")
        self.analyze_all_btn = ttk.Button(action_row, text="Analyze all", command=self.analyze_all)
        self.analyze_all_btn.pack(side="right", padx=(0, 8))

    def choose_source(self) -> None:
        path = filedialog.askdirectory(title="Choose invoice folder")
        if path:
            self.source_var.set(path)
            if self.output_var.get().endswith("invoice_results.csv"):
                self.output_var.set(str(Path(path) / "invoice_results.csv"))
            self.scan_folder()

    def choose_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save analysis results",
            defaultextension=".csv",
            filetypes=[("CSV file", "*.csv")],
            initialfile="invoice_results.csv",
        )
        if path:
            self.output_var.set(path)

    def scan_folder(self) -> None:
        source = self.source_var.get().strip()
        if not source:
            messagebox.showinfo(APP_TITLE, "Choose an invoice folder first.")
            return
        base = Path(source)
        if not base.is_dir():
            messagebox.showerror(APP_TITLE, "The selected invoice folder does not exist.")
            return
        self.files = find_pdfs(base, recursive=self.recursive_var.get())
        self.file_tree.delete(*self.file_tree.get_children())
        for index, path in enumerate(self.files):
            size_mb = path.stat().st_size / (1024 * 1024)
            self.file_tree.insert("", "end", iid=str(index), values=(path.name, f"{size_mb:.2f} MB"))
        self.select_all_files()
        self.current_file_var.set(f"Found {len(self.files)} PDF invoice(s).")
        self.progress_text.set("Ready")
        self.progress["value"] = 0
        self.summary_vars["Files"].set(str(len(self.files)))

    def select_all_files(self) -> None:
        items = self.file_tree.get_children()
        self.file_tree.selection_set(items)
        self._update_selected_count()

    def clear_file_selection(self) -> None:
        self.file_tree.selection_remove(self.file_tree.selection())
        self._update_selected_count()

    def _update_selected_count(self) -> None:
        self.selected_count_var.set(f"{len(self.file_tree.selection())} selected")

    def _selected_paths(self) -> list[Path]:
        out: list[Path] = []
        for iid in self.file_tree.selection():
            try:
                out.append(self.files[int(iid)])
            except (ValueError, IndexError):
                continue
        return out

    def analyze_selected(self) -> None:
        self._start_analysis(self._selected_paths())

    def analyze_all(self) -> None:
        self._start_analysis(self.files)

    def _start_analysis(self, paths: list[Path]) -> None:
        if self.processing:
            return
        if not paths:
            messagebox.showinfo(APP_TITLE, "Select at least one invoice.")
            return
        output = self.output_var.get().strip()
        if not output:
            messagebox.showinfo(APP_TITLE, "Choose a results CSV location.")
            return
        output_path = Path(output)
        if output_path.suffix.lower() != ".csv":
            output_path = output_path.with_suffix(".csv")
            self.output_var.set(str(output_path))
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Unable to create the output folder:\n{exc}")
            return

        self.processing = True
        self.results = []
        self.result_tree.delete(*self.result_tree.get_children())
        self._set_details("Processing invoices…")
        self.progress["value"] = 0
        self.progress_text.set(f"0 / {len(paths)} · estimating time")
        self.current_file_var.set("Starting analysis…")
        self.analyze_selected_btn.state(["disabled"])
        self.analyze_all_btn.state(["disabled"])

        force = bool(self.force_var.get())
        thread = threading.Thread(
            target=self._analysis_worker,
            args=(paths, output_path, force),
            daemon=True,
        )
        thread.start()

    def _analysis_worker(self, paths: list[Path], output_path: Path, force: bool) -> None:
        db_path = output_path.with_name("invoice_intelligence.db")
        engine = None
        try:
            engine = InvoiceEngine(str(db_path))

            def progress(payload: dict) -> None:
                self.events.put(("progress", payload))

            results = engine.process_files(
                paths,
                force=force,
                progress=progress,
            )
            engine.export_csv(results, str(output_path))
            self.events.put(("done", {"results": results, "csv": str(output_path), "db": str(db_path)}))
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            if engine:
                engine.close()

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress":
                    self._handle_progress(payload)  # type: ignore[arg-type]
                elif event == "done":
                    self._handle_done(payload)  # type: ignore[arg-type]
                elif event == "error":
                    self._handle_error(str(payload))
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _handle_progress(self, payload: dict) -> None:
        fraction = float(payload.get("fraction", 0.0))
        index = int(payload.get("index", 0))
        total = int(payload.get("total", 0))
        eta = float(payload.get("eta_seconds", 0.0))
        current = str(payload.get("current_file", ""))
        skipped = bool(payload.get("skipped", False))
        result = payload.get("result")
        self.progress["value"] = fraction * 100.0
        self.current_file_var.set(("Cached: " if skipped else "Analyzed: ") + current)
        self.progress_text.set(f"{index} / {total} · ETA {format_seconds(eta)}")
        if isinstance(result, InvoiceResult):
            self.results.append(result)
            self._insert_result(result)
            self._update_summary(self.results)

    def _handle_done(self, payload: dict) -> None:
        self.processing = False
        self.analyze_selected_btn.state(["!disabled"])
        self.analyze_all_btn.state(["!disabled"])
        self.progress["value"] = 100
        self.progress_text.set(f"Complete · {len(payload['results'])} invoice(s)")
        self.current_file_var.set(f"Saved CSV: {payload['csv']}")
        self._update_summary(payload["results"])
        if not payload["results"]:
            self._set_details("No invoices were processed.")

    def _handle_error(self, message: str) -> None:
        self.processing = False
        self.analyze_selected_btn.state(["!disabled"])
        self.analyze_all_btn.state(["!disabled"])
        self.progress_text.set("Stopped")
        messagebox.showerror(APP_TITLE, message)

    def _insert_result(self, item: InvoiceResult) -> None:
        amount = f"${item.total_amount_due:,.2f}" if item.total_amount_due is not None else "—"
        confidence = f"{item.confidence:.0%}" if item.confidence else "—"
        if item.status == STATUS_OK:
            tag = "ok"
        elif item.status == STATUS_DUPLICATE:
            tag = "duplicate"
        elif item.status == STATUS_FAILED:
            tag = "failed"
        else:
            tag = "review"
        iid = str(len(self.result_tree.get_children()))
        self.result_tree.insert(
            "", "end", iid=iid,
            values=(item.vendor, item.account_number or "—", item.bill_date or "—", amount, item.meter_number or "—", confidence, item.status),
            tags=(tag,),
        )

    def _update_summary(self, results: list[InvoiceResult]) -> None:
        self.summary_vars["Files"].set(str(len(results)))
        total_amount = sum(float(item.total_amount_due) for item in results if item.total_amount_due is not None and item.status != STATUS_DUPLICATE)
        self.summary_vars["Amount"].set(f"${total_amount:,.2f}")
        self.summary_vars["OK"].set(str(sum(item.status == STATUS_OK for item in results)))
        self.summary_vars["Review"].set(str(sum(item.status in {STATUS_REVIEW, STATUS_FAILED} for item in results)))
        self.summary_vars["Duplicates"].set(str(sum(item.status == STATUS_DUPLICATE for item in results)))

    def _show_result_details(self, _event=None) -> None:
        selection = self.result_tree.selection()
        if not selection:
            return
        try:
            item = self.results[int(selection[0])]
        except (ValueError, IndexError):
            return
        lines = [f"{item.file_name}", f"Status: {item.status}    Confidence: {item.confidence:.1%}"]
        if item.account_number:
            lines.append(f"Account: {item.account_number}")
        if item.total_amount_due is not None:
            lines.append(f"Total due: ${item.total_amount_due:,.2f}")
        if item.current_charges is not None:
            lines.append(f"Current charges: ${item.current_charges:,.2f}")
        if item.meter_number:
            lines.append(f"Meter: {item.meter_number}")
        if item.duplicate_of:
            lines.append(f"Duplicate of: {item.duplicate_of}")
        if item.issues:
            lines.append("\nReview required:")
            lines.extend(f"• {message}" for message in item.issues)
        if item.warnings:
            lines.append("\nWarnings:")
            lines.extend(f"• {message}" for message in item.warnings)
        if not item.issues and not item.warnings:
            lines.append("\nNo validation exceptions detected.")
        self._set_details("\n".join(lines))

    def _set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def open_selected_result(self) -> None:
        selection = self.result_tree.selection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Select a result first.")
            return
        try:
            result = self.results[int(selection[0])]
        except (ValueError, IndexError):
            return
        open_file(result.file_path)


def main() -> None:
    app = InvoiceApp()
    app.mainloop()


if __name__ == "__main__":
    main()
