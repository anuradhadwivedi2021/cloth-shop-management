# core/importer.py
import csv

def preview_csv_import(file_path, limit=5):
    """Reads first few rows of CSV for preview before final import."""
    preview_data = []
    try:
        with open(file_path, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for i, row in enumerate(reader):
                if i >= limit:
                    break
                preview_data.append(row)
        return header, preview_data
    except Exception as e:
        return None, str(e)