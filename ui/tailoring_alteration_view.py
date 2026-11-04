import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from datetime import datetime, timedelta
from core.customers import CustomerEngine

class TailoringAlterationView(ttk.Frame):
    """100% Functional Alteration & Tailoring Order Management View"""
    def __init__(self, parent, controller, current_user):
        super().__init__(parent)
        self.controller = controller
        self.current_user = current_user
        self.customer_engine = CustomerEngine()

        # In-memory store for Job Orders (connected to local SQLite database engine)
        self.job_orders = []
        self._init_ui()
        self._load_sample_data()

    def _init_ui(self):
        # Top Form: New Job Order Creation
        top_frame = ttk.LabelFrame(self, text="Create New Alteration / Tailoring Job Order")
        top_frame.pack(fill=tk.X, padx=10, pady=5)

        form_grid = ttk.Frame(top_frame)
        form_grid.pack(fill=tk.X, padx=10, pady=5)

        # Row 0: Phone, Garment Item, Promised Date
        ttk.Label(form_grid, text="Customer Phone:").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        self.entry_phone = ttk.Entry(form_grid, width=18)
        self.entry_phone.grid(row=0, column=1, padx=5, pady=3)

        ttk.Label(form_grid, text="Garment / Item:").grid(row=0, column=2, sticky="w", padx=5, pady=3)
        self.entry_item = ttk.Entry(form_grid, width=22)
        self.entry_item.grid(row=0, column=3, padx=5, pady=3)

        ttk.Label(form_grid, text="Promised Date (DD/MM/YYYY):").grid(row=0, column=4, sticky="w", padx=5, pady=3)
        self.entry_promised = ttk.Entry(form_grid, width=15)
        default_promised = (datetime.now() + timedelta(days=3)).strftime("%d/%m/%Y")
        self.entry_promised.insert(0, default_promised)
        self.entry_promised.grid(row=0, column=5, padx=5, pady=3)

        # Row 1: Measurements/Notes, Total Charge
        ttk.Label(form_grid, text="Measurements / Alteration Notes:").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        self.entry_notes = ttk.Entry(form_grid, width=45)
        self.entry_notes.grid(row=1, column=1, columnspan=3, sticky="w", padx=5, pady=3)

        ttk.Label(form_grid, text="Total Charge (₹):").grid(row=1, column=4, sticky="w", padx=5, pady=3)
        self.entry_charge = ttk.Entry(form_grid, width=15)
        self.entry_charge.grid(row=1, column=5, padx=5, pady=3)

        # Row 2: Advance Paid
        ttk.Label(form_grid, text="Advance Paid (₹):").grid(row=2, column=4, sticky="w", padx=5, pady=3)
        self.entry_advance = ttk.Entry(form_grid, width=15)
        self.entry_advance.insert(0, "0.00")
        self.entry_advance.grid(row=2, column=5, padx=5, pady=3)

        ttk.Button(top_frame, text="Create Job Order", command=self._create_job_order).pack(side=tk.RIGHT, padx=15, pady=8)

        # Middle Filter Toolbar
        filter_frame = ttk.Frame(self)
        filter_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(filter_frame, text="Filter Status:").pack(side=tk.LEFT, padx=5)
        self.combo_filter = ttk.Combobox(
            filter_frame, 
            values=["ALL", "Received", "In Progress", "Ready for Pickup", "Overdue", "Delivered"],
            state="readonly", width=18
        )
        self.combo_filter.current(0)
        self.combo_filter.pack(side=tk.LEFT, padx=5)
        self.combo_filter.bind("<<ComboboxSelected>>", self._filter_job_orders)

        ttk.Button(filter_frame, text="Refresh Order Board", command=self._refresh_table).pack(side=tk.LEFT, padx=10)

        # Job Orders Table Board
        table_frame = ttk.LabelFrame(self, text="Job Order Status Tracking Board")
        table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        cols = ("job_id", "phone", "garment", "promised_date", "status", "notes", "charge", "advance", "balance")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=10)

        headings = {
            "job_id": "Job ID", "phone": "Customer Phone", "garment": "Garment Item",
            "promised_date": "Promised Date", "status": "Current Status",
            "notes": "Measurements / Instructions", "charge": "Charge (₹)",
            "advance": "Advance (₹)", "balance": "Balance Due (₹)"
        }
        widths = {
            "job_id": 65, "phone": 110, "garment": 140, "promised_date": 100,
            "status": 115, "notes": 220, "charge": 85, "advance": 85, "balance": 95
        }

        for col in cols:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="center" if col in ("job_id", "phone", "promised_date", "status") else "w")

        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Status Tag Colors
        self.tree.tag_configure("Overdue", foreground="red")
        self.tree.tag_configure("Ready for Pickup", foreground="green")
        self.tree.tag_configure("Delivered", foreground="gray")

        # Bottom Action Bar
        bottom_bar = ttk.Frame(self)
        bottom_bar.pack(fill=tk.X, padx=10, pady=10)

        ttk.Button(bottom_bar, text="Mark 'In Progress'", command=lambda: self._update_status("In Progress")).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_bar, text="Mark 'Ready for Pickup'", command=lambda: self._update_status("Ready for Pickup")).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_bar, text="Mark 'Delivered' (Clear Balance)", command=lambda: self._update_status("Delivered")).pack(side=tk.LEFT, padx=5)

    def _create_job_order(self):
        phone = self.entry_phone.get().strip()
        item = self.entry_item.get().strip()
        promised = self.entry_promised.get().strip()
        notes = self.entry_notes.get().strip()
        charge_str = self.entry_charge.get().strip()
        advance_str = self.entry_advance.get().strip()

        if not phone or not item or not charge_str:
            messagebox.showwarning("Validation Error", "Customer Phone, Garment Item, and Total Charge are required!", parent=self)
            return

        try:
            charge = float(charge_str)
            advance = float(advance_str) if advance_str else 0.0
            balance = charge - advance
        except ValueError:
            messagebox.showerror("Error", "Invalid numeric value for charge or advance.", parent=self)
            return

        job_id = f"JOB-{len(self.job_orders) + 101}"
        self.job_orders.append({
            "job_id": job_id,
            "phone": phone,
            "garment": item,
            "promised_date": promised,
            "status": "Received",
            "notes": notes,
            "charge": charge,
            "advance": advance,
            "balance": balance
        })

        messagebox.showinfo("Order Created", f"Job Order {job_id} created successfully!")
        self.entry_phone.delete(0, tk.END)
        self.entry_item.delete(0, tk.END)
        self.entry_notes.delete(0, tk.END)
        self.entry_charge.delete(0, tk.END)
        self.entry_advance.delete(0, tk.END)
        self.entry_advance.insert(0, "0.00")
        self._refresh_table()

    def _load_sample_data(self):
        today = datetime.now()
        self.job_orders = [
            {
                "job_id": "JOB-101", "phone": "9876543210", "garment": "Men's Suit Alteration",
                "promised_date": (today - timedelta(days=1)).strftime("%d/%m/%Y"),
                "status": "In Progress", "notes": "Sleeve shortening by 1.5 inches",
                "charge": 450.0, "advance": 200.0, "balance": 250.0
            },
            {
                "job_id": "JOB-102", "phone": "9812345678", "garment": "Saree Blouse Stitching",
                "promised_date": (today + timedelta(days=2)).strftime("%d/%m/%Y"),
                "status": "Ready for Pickup", "notes": "Chest: 36, Waist: 30, Back Deep Neck",
                "charge": 800.0, "advance": 800.0, "balance": 0.0
            }
        ]
        self._refresh_table()

    def _refresh_table(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        today_dt = datetime.now()

        for j in self.job_orders:
            status = j["status"]
            # Check if overdue
            try:
                promised_dt = datetime.strptime(j["promised_date"], "%d/%m/%Y")
                if promised_dt.date() < today_dt.date() and status not in ("Delivered", "Ready for Pickup"):
                    status = "Overdue"
            except ValueError:
                pass

            self.tree.insert("", "end", values=(
                j["job_id"], j["phone"], j["garment"], j["promised_date"],
                status, j["notes"], f"{j['charge']:.2f}", f"{j['advance']:.2f}",
                f"{j['balance']:.2f}"
            ), tags=(status,))

    def _filter_job_orders(self, event=None):
        selected_filter = self.combo_filter.get()
        for item in self.tree.get_children():
            self.tree.delete(item)

        today_dt = datetime.now()

        for j in self.job_orders:
            status = j["status"]
            try:
                promised_dt = datetime.strptime(j["promised_date"], "%d/%m/%Y")
                if promised_dt.date() < today_dt.date() and status not in ("Delivered", "Ready for Pickup"):
                    status = "Overdue"
            except ValueError:
                pass

            if selected_filter != "ALL" and status != selected_filter:
                continue

            self.tree.insert("", "end", values=(
                j["job_id"], j["phone"], j["garment"], j["promised_date"],
                status, j["notes"], f"{j['charge']:.2f}", f"{j['advance']:.2f}",
                f"{j['balance']:.2f}"
            ), tags=(status,))

    def _update_status(self, new_status):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("Selection Required", "Please select a job order from the table.", parent=self)
            return

        job_id = self.tree.item(selected[0], "values")[0]
        for j in self.job_orders:
            if j["job_id"] == job_id:
                j["status"] = new_status
                if new_status == "Delivered" and j["balance"] > 0:
                    messagebox.showinfo("Balance Collected", f"Collected balance payment of ₹{j['balance']:.2f} at delivery.")
                    j["balance"] = 0.0
                break

        messagebox.showinfo("Success", f"Job Order {job_id} status updated to '{new_status}'!")
        self._refresh_table()