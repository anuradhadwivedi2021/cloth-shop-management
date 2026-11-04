import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from core.products import ProductEngine
from core.printing_export import LabelPrinterEngine

class BarcodeLabelView(ttk.Frame):
    """100% Functional Barcode Label Designer & Batch Thermal Printing View"""
    def __init__(self, parent, controller, current_user):
        super().__init__(parent)
        self.controller = controller
        self.current_user = current_user
        self.product_engine = ProductEngine()
        self.label_printer = LabelPrinterEngine()

        self.print_queue = []  # List of dicts: {variant_id, name, size, colour, price, mrp, barcode, print_qty}

        self._init_ui()

    def _init_ui(self):
        # Header
        top_frame = ttk.LabelFrame(self, text="Barcode Label Printing & Designer Setup")
        top_frame.pack(fill=tk.X, padx=10, pady=5)

        # Left Panel: Printer & Label Config
        config_frame = ttk.LabelFrame(top_frame, text="Label Configuration")
        config_frame.pack(side=tk.LEFT, padx=10, pady=5, fill=tk.Y)

        ttk.Label(config_frame, text="Label Size:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.combo_size = ttk.Combobox(config_frame, values=["50x25 mm (Standard)", "38x25 mm (Small)", "50x38 mm (Large)"], state="readonly")
        self.combo_size.current(0)
        self.combo_size.grid(row=0, column=1, padx=5, pady=5)

        self.var_show_shop = tk.BooleanVar(value=True)
        self.var_show_size_col = tk.BooleanVar(value=True)
        self.var_show_price = tk.BooleanVar(value=True)
        self.var_show_mrp = tk.BooleanVar(value=True)

        ttk.Checkbutton(config_frame, text="Show Shop Name", variable=self.var_show_shop).grid(row=1, column=0, columnspan=2, sticky="w", padx=5)
        ttk.Checkbutton(config_frame, text="Show Size & Colour", variable=self.var_show_size_col).grid(row=2, column=0, columnspan=2, sticky="w", padx=5)
        ttk.Checkbutton(config_frame, text="Show Selling Price", variable=self.var_show_price).grid(row=3, column=0, columnspan=2, sticky="w", padx=5)
        ttk.Checkbutton(config_frame, text="Show MRP", variable=self.var_show_mrp).grid(row=4, column=0, columnspan=2, sticky="w", padx=5)

        # Right Panel: Add Item to Print Queue
        add_frame = ttk.LabelFrame(top_frame, text="Search Variant to Print")
        add_frame.pack(side=tk.LEFT, padx=10, pady=5, fill=tk.BOTH, expand=True)

        ttk.Label(add_frame, text="Barcode / SKU / Name:").pack(side=tk.LEFT, padx=5, pady=5)
        self.entry_search = ttk.Entry(add_frame, width=25, font=("Arial", 10))
        self.entry_search.pack(side=tk.LEFT, padx=5, pady=5)
        self.entry_search.bind("<Return>", self._search_and_add_variant)

        ttk.Button(add_frame, text="Search & Add", command=self._search_and_add_variant).pack(side=tk.LEFT, padx=5)

        # Batch Queue Table
        table_frame = ttk.LabelFrame(self, text="Batch Label Printing Queue")
        table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        cols = ("variant_id", "name", "size", "colour", "barcode", "price", "mrp", "print_qty")
        self.queue_tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=10)

        headings = {
            "variant_id": "ID", "name": "Product Name", "size": "Size", "colour": "Colour",
            "barcode": "Barcode / SKU", "price": "Price (₹)", "mrp": "MRP (₹)", "print_qty": "Print Quantity"
        }
        widths = {"variant_id": 50, "name": 200, "size": 70, "colour": 90, "barcode": 130, "price": 90, "mrp": 90, "print_qty": 100}

        for col in cols:
            self.queue_tree.heading(col, text=headings[col])
            self.queue_tree.column(col, width=widths[col], anchor="center" if col not in ("name",) else "w")

        self.queue_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.queue_tree.yview)
        self.queue_tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Bottom Actions Bar
        bottom_bar = ttk.Frame(self)
        bottom_bar.pack(fill=tk.X, padx=10, pady=10)

        self.lbl_total_labels = ttk.Label(bottom_bar, text="Total Labels to Print: 0", font=("Arial", 11, "bold"))
        self.lbl_total_labels.pack(side=tk.LEFT, padx=10)

        ttk.Button(bottom_bar, text="Remove Selected", command=self._remove_selected).pack(side=tk.RIGHT, padx=5)
        ttk.Button(bottom_bar, text="Clear Queue", command=self._clear_queue).pack(side=tk.RIGHT, padx=5)
        ttk.Button(bottom_bar, text="Print Batch Labels", command=self._print_batch, style="Accent.TButton").pack(side=tk.RIGHT, padx=5)

    def _search_and_add_variant(self, event=None):
        query = self.entry_search.get().strip()
        if not query:
            return

        variant = self.product_engine.get_variant_by_code(query)
        if not variant:
            products = self.product_engine.search_products(query)
            if products:
                variants = self.product_engine.get_product_variants(products[0]["id"])
                if variants:
                    variant = variants[0]

        if not variant:
            messagebox.showerror("Not Found", f"No product variant found matching '{query}'", parent=self)
            return

        qty_str = simpledialog.askstring("Print Quantity", f"Enter label quantity for {variant.get('name', 'Item')} ({variant['size']}/{variant['colour']}):", initialvalue="1", parent=self)
        if not qty_str or not qty_str.isdigit() or int(qty_str) <= 0:
            return

        print_qty = int(qty_str)

        # Add to queue
        self.print_queue.append({
            "variant_id": variant["id"],
            "name": variant.get("product_name", variant.get("name", "Clothing Item")),
            "size": variant["size"],
            "colour": variant["colour"],
            "barcode": variant.get("barcode", f"SKU-{variant['id']}"),
            "price": float(variant["price"]),
            "mrp": float(variant.get("mrp", variant["price"])),
            "print_qty": print_qty
        })

        self.entry_search.delete(0, tk.END)
        self._refresh_queue_display()

    def _refresh_queue_display(self):
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)

        total_count = 0
        for item in self.print_queue:
            total_count += item["print_qty"]
            self.queue_tree.insert("", "end", values=(
                item["variant_id"], item["name"], item["size"], item["colour"],
                item["barcode"], f"{item['price']:.2f}", f"{item['mrp']:.2f}", item["print_qty"]
            ))

        self.lbl_total_labels.config(text=f"Total Labels to Print: {total_count}")

    def _remove_selected(self):
        selected = self.queue_tree.selection()
        if not selected:
            return
        
        idx = self.queue_tree.index(selected[0])
        del self.print_queue[idx]
        self._refresh_queue_display()

    def _clear_queue(self):
        if self.print_queue and messagebox.askyesno("Clear Queue", "Are you sure you want to clear the label batch queue?"):
            self.print_queue.clear()
            self._refresh_queue_display()

    def _print_batch(self):
        if not self.print_queue:
            messagebox.showwarning("Empty Queue", "No labels added to queue for printing!", parent=self)
            return

        config = {
            "size": self.combo_size.get(),
            "show_shop": self.var_show_shop.get(),
            "show_size_col": self.var_show_size_col.get(),
            "show_price": self.var_show_price.get(),
            "show_mrp": self.var_show_mrp.get()
        }

        success = self.label_printer.send_batch_to_thermal_printer(self.print_queue, config)
        if success:
            messagebox.showinfo("Success", "Barcode batch sent to thermal printer successfully!")
            self.print_queue.clear()
            self._refresh_queue_display()
        else:
            messagebox.showerror("Printer Error", "Failed to print labels. Check printer connection.")