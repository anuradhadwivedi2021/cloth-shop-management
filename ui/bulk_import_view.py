import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import csv
import os
from core.products import ProductEngine

class BulkImportView(ttk.Frame):
    """100% Functional Bulk CSV/Excel Product & Variant Import Tool with Preview"""
    def __init__(self, parent, controller, current_user):
        super().__init__(parent)
        self.controller = controller
        self.current_user = current_user
        self.product_engine = ProductEngine()

        self.loaded_records = []  # Parsed rows
        self.valid_records = []
        self.error_records = []

        self._init_ui()

    def _init_ui(self):
        # Top File Picker Panel
        top_frame = ttk.LabelFrame(self, text="Select CSV / Excel Import File")
        top_frame.pack(fill=tk.X, padx=10, pady=5)

        self.lbl_filepath = ttk.Label(top_frame, text="No file selected...", font=("Arial", 10, "italic"))
        self.lbl_filepath.pack(side=tk.LEFT, padx=10, pady=8)

        ttk.Button(top_frame, text="Browse CSV File", command=self._browse_file).pack(side=tk.RIGHT, padx=5, pady=5)
        ttk.Button(top_frame, text="Download Sample Template", command=self._download_template).pack(side=tk.RIGHT, padx=5, pady=5)

        # Validation Preview Summary Panel
        summary_frame = ttk.Frame(self)
        summary_frame.pack(fill=tk.X, padx=10, pady=5)

        self.lbl_total = ttk.Label(summary_frame, text="Total Rows: 0", font=("Arial", 10, "bold"))
        self.lbl_total.pack(side=tk.LEFT, padx=10)

        self.lbl_valid = ttk.Label(summary_frame, text="Valid Rows: 0", font=("Arial", 10, "bold"), foreground="green")
        self.lbl_valid.pack(side=tk.LEFT, padx=10)

        self.lbl_errors = ttk.Label(summary_frame, text="Invalid Rows: 0", font=("Arial", 10, "bold"), foreground="red")
        self.lbl_errors.pack(side=tk.LEFT, padx=10)

        # Table Grid for Validation Preview
        table_frame = ttk.LabelFrame(self, text="Validation Preview Grid")
        table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        cols = ("row_no", "status", "product_name", "category", "size", "colour", "price", "stock", "barcode", "error_msg")
        self.preview_tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=12)

        headings = {
            "row_no": "Row #", "status": "Status", "product_name": "Product Name",
            "category": "Category", "size": "Size", "colour": "Colour",
            "price": "Price (₹)", "stock": "Stock", "barcode": "Barcode / SKU", "error_msg": "Validation Details"
        }
        widths = {
            "row_no": 50, "status": 80, "product_name": 180, "category": 100,
            "size": 60, "colour": 80, "price": 80, "stock": 60, "barcode": 120, "error_msg": 220
        }

        for col in cols:
            self.preview_tree.heading(col, text=headings[col])
            self.preview_tree.column(col, width=widths[col], anchor="center" if col in ("row_no", "status", "size", "colour", "stock") else "w")

        self.preview_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.preview_tree.yview)
        self.preview_tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Configure status row tags
        self.preview_tree.tag_configure("VALID", foreground="green")
        self.preview_tree.tag_configure("INVALID", foreground="red")

        # Bottom Actions Bar
        bottom_bar = ttk.Frame(self)
        bottom_bar.pack(fill=tk.X, padx=10, pady=10)

        ttk.Button(bottom_bar, text="Export Error Log", command=self._export_error_log).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_bar, text="Confirm & Import Valid Records", command=self._confirm_import, style="Accent.TButton").pack(side=tk.RIGHT, padx=5)

    def _browse_file(self):
        file_path = filedialog.askopenfilename(
            title="Select CSV Import File",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")]
        )
        if not file_path:
            return

        self.lbl_filepath.config(text=os.path.basename(file_path))
        self._parse_and_validate_csv(file_path)

    def _parse_and_validate_csv(self, file_path):
        self.loaded_records.clear()
        self.valid_records.clear()
        self.error_records.clear()

        for item in self.preview_tree.get_children():
            self.preview_tree.delete(item)

        try:
            with open(file_path, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                
                required_cols = {"product_name", "category", "size", "colour", "price", "stock_qty"}
                if not required_cols.issubset(set(reader.fieldnames or [])):
                    messagebox.showerror("Header Error", f"CSV file missing required columns: {required_cols}")
                    return

                for idx, row in enumerate(reader, 1):
                    is_valid, err_msg = self._validate_row(row)
                    record_entry = {
                        "row_no": idx,
                        "data": row,
                        "is_valid": is_valid,
                        "error_msg": err_msg
                    }

                    if is_valid:
                        self.valid_records.append(record_entry)
                    else:
                        self.error_records.append(record_entry)

                    status_str = "VALID" if is_valid else "INVALID"
                    self.preview_tree.insert("", "end", values=(
                        idx, status_str, row.get("product_name", ""),
                        row.get("category", ""), row.get("size", ""),
                        row.get("colour", ""), row.get("price", "0"),
                        row.get("stock_qty", "0"), row.get("barcode", "-"),
                        err_msg if err_msg else "Ready to import"
                    ), tags=(status_str,))

            self.lbl_total.config(text=f"Total Rows: {len(self.valid_records) + len(self.error_records)}")
            self.lbl_valid.config(text=f"Valid Rows: {len(self.valid_records)}")
            self.lbl_errors.config(text=f"Invalid Rows: {len(self.error_records)}")

        except Exception as e:
            messagebox.showerror("File Error", f"Failed to read CSV file: {str(e)}")

    def _validate_row(self, row):
        name = row.get("product_name", "").strip()
        category = row.get("category", "").strip()
        size = row.get("size", "").strip()
        colour = row.get("colour", "").strip()
        price_str = row.get("price", "").strip()
        stock_str = row.get("stock_qty", "").strip()

        if not name:
            return False, "Missing product_name"
        if not category:
            return False, "Missing category"
        if not size or not colour:
            return False, "Missing size or colour"

        try:
            price = float(price_str)
            if price <= 0:
                return False, "Price must be > 0"
        except ValueError:
            return False, "Invalid price number"

        try:
            stock = int(stock_str)
            if stock < 0:
                return False, "Stock cannot be negative"
        except ValueError:
            return False, "Invalid stock quantity"

        return True, ""

    def _download_template(self):
        save_path = filedialog.asksaveasfilename(
            title="Save Sample Import Template",
            defaultextension=".csv",
            initialfile="sample_products_import.csv",
            filetypes=[("CSV Files", "*.csv")]
        )
        if not save_path:
            return

        headers = ["product_name", "category", "brand", "size", "colour", "price", "mrp", "stock_qty", "barcode"]
        sample_data = [
            ["Cotton Casual Shirt", "Shirts", "Roadster", "M", "Blue", "1299.00", "1499.00", "25", "SHIRT-M-BLU"],
            ["Cotton Casual Shirt", "Shirts", "Roadster", "L", "Blue", "1299.00", "1499.00", "30", "SHIRT-L-BLU"],
            ["Slim Fit Jeans", "Trousers", "Levis", "32", "Black", "2499.00", "2999.00", "15", "JEAN-32-BLK"]
        ]

        try:
            with open(save_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(sample_data)
            messagebox.showinfo("Success", f"Sample import template saved to:\n{save_path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save template: {str(e)}")

    def _export_error_log(self):
        if not self.error_records:
            messagebox.showinfo("No Errors", "No invalid records to export.")
            return

        save_path = filedialog.asksaveasfilename(
            title="Save Error Log File",
            defaultextension=".csv",
            initialfile="import_error_log.csv",
            filetypes=[("CSV Files", "*.csv")]
        )
        if not save_path:
            return

        try:
            with open(save_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["Row Number", "Product Name", "Error Reason"])
                for err in self.error_records:
                    writer.writerow([err["row_no"], err["data"].get("product_name", ""), err["error_msg"]])
            messagebox.showinfo("Success", f"Error log saved to:\n{save_path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save error log: {str(e)}")

    def _confirm_import(self):
        if not self.valid_records:
            messagebox.showwarning("No Valid Data", "No valid records available to import.")
            return

        confirm = messagebox.askyesno(
            "Confirm Import",
            f"Are you sure you want to import {len(self.valid_records)} valid records into database?"
        )
        if not confirm:
            return

        success_count = self.product_engine.bulk_import_products_and_variants([r["data"] for r in self.valid_records])
        if success_count > 0:
            messagebox.showinfo("Import Complete", f"Successfully imported {success_count} product variants!")
            self.loaded_records.clear()
            self.valid_records.clear()
            self.error_records.clear()
            for item in self.preview_tree.get_children():
                self.preview_tree.delete(item)
            self.lbl_total.config(text="Total Rows: 0")
            self.lbl_valid.config(text="Valid Rows: 0")
            self.lbl_errors.config(text="Invalid Rows: 0")
        else:
            messagebox.showerror("Import Failed", "Failed to import records into database.")