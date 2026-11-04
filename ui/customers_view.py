import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from core.customers import CustomerEngine
from core.vouchers_loyalty import VoucherLoyaltyEngine

class CustomerLoyaltyView(ttk.Frame):
    """100% Functional Customer Management, DPDP Consent & Loyalty/Voucher UI"""
    def __init__(self, parent, controller, current_user):
        super().__init__(parent)
        self.controller = controller
        self.current_user = current_user
        self.customer_engine = CustomerEngine()
        self.loyalty_engine = VoucherLoyaltyEngine()

        self.selected_customer_id = None
        self._init_ui()

    def _init_ui(self):
        # Top Panel: Customer Profile & DPDP Registration
        top_frame = ttk.LabelFrame(self, text="Customer Registration & DPDP Privacy Consent")
        top_frame.pack(fill=tk.X, padx=10, pady=5)

        # Left Form
        form_frame = ttk.Frame(top_frame)
        form_frame.pack(side=tk.LEFT, padx=10, pady=5)

        ttk.Label(form_frame, text="Full Name:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.entry_name = ttk.Entry(form_frame, width=22)
        self.entry_name.grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(form_frame, text="Phone Number:").grid(row=0, column=2, sticky="w", padx=5, pady=2)
        self.entry_phone = ttk.Entry(form_frame, width=20)
        self.entry_phone.grid(row=0, column=3, padx=5, pady=2)

        ttk.Label(form_frame, text="GSTIN (Optional):").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.entry_gstin = ttk.Entry(form_frame, width=22)
        self.entry_gstin.grid(row=1, column=1, padx=5, pady=2)

        # DPDP Consent Flag
        self.var_dpdp_consent = tk.BooleanVar(value=True)
        self.chk_dpdp = ttk.Checkbutton(
            form_frame, 
            text="Obtained DPDP Privacy Consent (Store name & phone for billing & receipts)", 
            variable=self.var_dpdp_consent
        )
        self.chk_dpdp.grid(row=2, column=0, columnspan=4, sticky="w", padx=5, pady=5)

        ttk.Button(top_frame, text="Save Customer Profile", command=self._save_customer).pack(side=tk.RIGHT, padx=15, pady=10)

        # Main Split Frame: Customer Table (Left) & Loyalty / Voucher Manager (Right)
        main_split = ttk.Frame(self)
        main_split.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Customer List Table
        left_table_frame = ttk.LabelFrame(main_split, text="Registered Customers")
        left_table_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        cols = ("id", "name", "phone", "points", "store_credit", "dpdp_status")
        self.cust_tree = ttk.Treeview(left_table_frame, columns=cols, show="headings", height=10)

        headings = {"id": "ID", "name": "Name", "phone": "Phone", "points": "Loyalty Points", "store_credit": "Store Credit (₹)", "dpdp_status": "DPDP Consent"}
        widths = {"id": 40, "name": 140, "phone": 100, "points": 90, "store_credit": 110, "dpdp_status": 100}

        for col in cols:
            self.cust_tree.heading(col, text=headings[col])
            self.cust_tree.column(col, width=widths[col], anchor="center" if col in ("id", "phone", "dpdp_status") else "w")

        self.cust_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.cust_tree.bind("<<TreeviewSelect>>", self._on_customer_selected)

        scrollbar = ttk.Scrollbar(left_table_frame, orient=tk.VERTICAL, command=self.cust_tree.yview)
        self.cust_tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Right Panel: Loyalty & Gift Voucher Actions
        right_action_frame = ttk.LabelFrame(main_split, text="Loyalty Points & Gift Voucher Controls")
        right_action_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=False, padx=5)

        # Selected Customer Info Label
        self.lbl_selected_cust = ttk.Label(right_action_frame, text="Selected: None", font=("Arial", 11, "bold"), foreground="navy")
        self.lbl_selected_cust.pack(pady=10, padx=10)

        # Loyalty Points Actions Box
        loyalty_box = ttk.LabelFrame(right_action_frame, text="Loyalty Points Actions")
        loyalty_box.pack(fill=tk.X, padx=10, pady=5)

        ttk.Button(loyalty_box, text="Add Manual Bonus Points", command=self._add_bonus_points).pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(loyalty_box, text="Redeem Points for Credit", command=self._redeem_points).pack(fill=tk.X, padx=10, pady=5)

        # Gift Voucher Actions Box
        voucher_box = ttk.LabelFrame(right_action_frame, text="Gift Voucher Engine")
        voucher_box.pack(fill=tk.X, padx=10, pady=10)

        ttk.Button(voucher_box, text="Issue New Gift Voucher", command=self._issue_voucher).pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(voucher_box, text="Verify Voucher Code Balance", command=self._verify_voucher).pack(fill=tk.X, padx=10, pady=5)

        self._load_customer_data()

    def _save_customer(self):
        name = self.entry_name.get().strip()
        phone = self.entry_phone.get().strip()
        gstin = self.entry_gstin.get().strip()
        dpdp_consent = self.var_dpdp_consent.get()

        if not name or not phone:
            messagebox.showwarning("Validation Error", "Customer Name and Phone Number are required!", parent=self)
            return

        cust_id = self.customer_engine.save_customer({
            "name": name,
            "phone": phone,
            "gstin": gstin,
            "dpdp_consent": 1 if dpdp_consent else 0,
            "consent_purpose": "Billing, GST compliance, and transaction receipt issuing"
        })

        if cust_id:
            messagebox.showinfo("Success", f"Customer '{name}' saved successfully with DPDP consent logged!")
            self.entry_name.delete(0, tk.END)
            self.entry_phone.delete(0, tk.END)
            self.entry_gstin.delete(0, tk.END)
            self._load_customer_data()
        else:
            messagebox.showerror("Error", "Failed to save customer record.", parent=self)

    def _load_customer_data(self):
        for item in self.cust_tree.get_children():
            self.cust_tree.delete(item)

        customers = self.customer_engine.get_all_customers()
        for c in customers:
            status = "CONSENTED" if c.get("dpdp_consent") == 1 else "NO CONSENT"
            self.cust_tree.insert("", "end", values=(
                c["id"], c["name"], c["phone"], c.get("points", 0),
                f"{c.get('store_credit', 0.0):.2f}", status
            ))

    def _on_customer_selected(self, event=None):
        selected = self.cust_tree.selection()
        if not selected:
            self.selected_customer_id = None
            self.lbl_selected_cust.config(text="Selected: None")
            return

        vals = self.cust_tree.item(selected[0], "values")
        self.selected_customer_id = int(vals[0])
        self.lbl_selected_cust.config(text=f"Selected: {vals[1]} ({vals[2]})")

    def _add_bonus_points(self):
        if not self.selected_customer_id:
            messagebox.showwarning("Selection Required", "Please select a customer first!", parent=self)
            return

        pts_str = simpledialog.askstring("Add Loyalty Points", "Enter Bonus Points to Add:", parent=self)
        if pts_str and pts_str.isdigit():
            pts = int(pts_str)
            self.loyalty_engine.add_loyalty_points(self.selected_customer_id, pts, reason="Manual Staff Bonus")
            messagebox.showinfo("Success", f"Added {pts} loyalty points to customer account!")
            self._load_customer_data()

    def _redeem_points(self):
        if not self.selected_customer_id:
            messagebox.showwarning("Selection Required", "Please select a customer first!", parent=self)
            return

        pts_str = simpledialog.askstring("Redeem Loyalty Points", "Enter Loyalty Points to Redeem:", parent=self)
        if pts_str and pts_str.isdigit():
            pts = int(pts_str)
            success = self.loyalty_engine.redeem_points_for_credit(self.selected_customer_id, pts)
            if success:
                messagebox.showinfo("Success", f"Redeemed {pts} points into store credit balance!")
                self._load_customer_data()
            else:
                messagebox.showerror("Error", "Insufficient loyalty points or invalid amount!")

    def _issue_voucher(self):
        amt_str = simpledialog.askstring("Issue Gift Voucher", "Enter Voucher Amount (₹):", parent=self)
        if not amt_str:
            return

        try:
            amt = float(amt_str)
            voucher_code = self.loyalty_engine.create_gift_voucher(amount=amt, customer_id=self.selected_customer_id)
            messagebox.showinfo("Voucher Issued", f"Gift Voucher Issued Successfully!\n\nVoucher Code: {voucher_code}\nValue: ₹{amt:.2f}")
        except ValueError:
            messagebox.showerror("Error", "Invalid amount entered.")

    def _verify_voucher(self):
        code = simpledialog.askstring("Verify Voucher", "Enter Gift Voucher Code:", parent=self)
        if not code:
            return

        details = self.loyalty_engine.get_voucher_details(code.strip().upper())
        if details and details.get("is_active"):
            messagebox.showinfo("Voucher Valid", f"Voucher Code: {details['code']}\nAvailable Balance: ₹{details['balance']:.2f}\nStatus: Active")
        else:
            messagebox.showerror("Invalid / Expired", "Voucher code is invalid, expired, or fully redeemed!")


class CustomersView:
    def __init__(self, *args, **kwargs):
        pass