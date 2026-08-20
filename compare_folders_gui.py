#!/usr/bin/env python3
"""
Standalone Folder Comparison Tool with Simple GUI
No Flask required - uses tkinter (built into Python)
"""
import sys
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import threading
from datetime import datetime

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import FolderComparer, NamingConventionComparer, ArchiveContentsComparer, JarManifestComparer

class ComparisonGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Folder Comparison Tool")
        self.root.geometry("900x700")
        
        # Configure grid weights
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(3, weight=1)
        
        # Header
        header = ttk.Label(root, text="Folder Comparison Tool", font=('Arial', 16, 'bold'))
        header.grid(row=0, column=0, padx=10, pady=10, sticky='w')
        
        # Input Frame
        input_frame = ttk.LabelFrame(root, text="Folder Paths", padding=10)
        input_frame.grid(row=1, column=0, padx=10, pady=5, sticky='ew')
        input_frame.grid_columnconfigure(1, weight=1)
        
        # Folder 1
        ttk.Label(input_frame, text="Folder 1:").grid(row=0, column=0, sticky='w', pady=5)
        self.folder1_var = tk.StringVar(value=r"\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06")
        folder1_entry = ttk.Entry(input_frame, textvariable=self.folder1_var, width=60)
        folder1_entry.grid(row=0, column=1, padx=5, pady=5, sticky='ew')
        ttk.Button(input_frame, text="Browse", command=lambda: self.browse_folder(self.folder1_var)).grid(row=0, column=2, padx=5)
        
        # Folder 2
        ttk.Label(input_frame, text="Folder 2:").grid(row=1, column=0, sticky='w', pady=5)
        self.folder2_var = tk.StringVar(value=r"\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf07")
        folder2_entry = ttk.Entry(input_frame, textvariable=self.folder2_var, width=60)
        folder2_entry.grid(row=1, column=1, padx=5, pady=5, sticky='ew')
        ttk.Button(input_frame, text="Browse", command=lambda: self.browse_folder(self.folder2_var)).grid(row=1, column=2, padx=5)
        
        # Options Frame
        options_frame = ttk.LabelFrame(root, text="Comparison Options", padding=10)
        options_frame.grid(row=2, column=0, padx=10, pady=5, sticky='ew')
        
        # Comparison mode
        ttk.Label(options_frame, text="Mode:").grid(row=0, column=0, sticky='w', pady=5)
        self.mode_var = tk.StringVar(value="naming")
        ttk.Radiobutton(options_frame, text="Naming Convention", variable=self.mode_var, value="naming").grid(row=0, column=1, sticky='w')
        ttk.Radiobutton(options_frame, text="Archives Content", variable=self.mode_var, value="archives").grid(row=0, column=2, sticky='w')
        ttk.Radiobutton(options_frame, text="Full Comparison", variable=self.mode_var, value="full").grid(row=0, column=3, sticky='w')
        
        # Use local copy
        self.use_local_copy_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_frame, text="Use Local Copy (faster for network paths)", 
                       variable=self.use_local_copy_var).grid(row=1, column=0, columnspan=4, sticky='w', pady=5)
        
        # Show all differences
        self.show_all_diff_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_frame, text="Show All Differences (not just missing)", 
                       variable=self.show_all_diff_var).grid(row=2, column=0, columnspan=4, sticky='w', pady=5)
        
        # Buttons
        button_frame = ttk.Frame(root)
        button_frame.grid(row=3, column=0, padx=10, pady=5, sticky='ew')
        
        self.run_button = ttk.Button(button_frame, text="Start Comparison", command=self.run_comparison)
        self.run_button.pack(side='left', padx=5)
        
        self.open_report_button = ttk.Button(button_frame, text="Open Last Report", command=self.open_last_report, state='disabled')
        self.open_report_button.pack(side='left', padx=5)
        
        ttk.Button(button_frame, text="Clear Log", command=self.clear_log).pack(side='left', padx=5)
        ttk.Button(button_frame, text="Exit", command=root.quit).pack(side='right', padx=5)
        
        # Log Frame
        log_frame = ttk.LabelFrame(root, text="Comparison Log", padding=10)
        log_frame.grid(row=4, column=0, padx=10, pady=5, sticky='nsew')
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)
        root.grid_rowconfigure(4, weight=1)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=20)
        self.log_text.grid(row=0, column=0, sticky='nsew')
        
        self.last_report_html = None
        
    def browse_folder(self, var):
        folder = filedialog.askdirectory()
        if folder:
            var.set(folder)
    
    def log(self, message):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()
    
    def clear_log(self):
        self.log_text.delete(1.0, tk.END)
    
    def open_last_report(self):
        if self.last_report_html and Path(self.last_report_html).exists():
            import webbrowser
            webbrowser.open(str(Path(self.last_report_html).absolute()))
        else:
            messagebox.showwarning("No Report", "No report available to open.")
    
    def run_comparison(self):
        # Disable button during comparison
        self.run_button.config(state='disabled')
        self.log("=" * 80)
        self.log("Starting comparison...")
        self.log("=" * 80)
        
        # Run in background thread to keep GUI responsive
        thread = threading.Thread(target=self._do_comparison, daemon=True)
        thread.start()
    
    def _do_comparison(self):
        try:
            folder1 = self.folder1_var.get().strip()
            folder2 = self.folder2_var.get().strip()
            mode = self.mode_var.get()
            use_local = self.use_local_copy_var.get()
            show_all = self.show_all_diff_var.get()
            
            if not folder1 or not folder2:
                self.log("ERROR: Both folder paths are required!")
                self.run_button.config(state='normal')
                return
            
            self.log(f"Folder 1: {folder1}")
            self.log(f"Folder 2: {folder2}")
            self.log(f"Mode: {mode}")
            self.log(f"Use local copy: {use_local}")
            self.log("")
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            if mode == "naming":
                self.log("Running Naming Convention Comparison...")
                comparer = NamingConventionComparer(folder1, folder2)
                comparer.similarity_threshold = 0.6
                comparer.use_local_copy = use_local
                comparer.max_retries = 5
                comparer.retry_delay = 1.0
                
                results = comparer.compare_naming_conventions()
                
                output_filename = f"ascf_naming_convention_report_{timestamp}.txt"
                comparer.save_naming_convention_report(output_filename)
                
                exact_matches = [r for r in results if r.is_exact_match]
                missing_from_folder2 = [r for r in results if r.exists_in_folder1 and not r.exists_in_folder2]
                missing_from_folder1 = [r for r in results if r.exists_in_folder2 and not r.exists_in_folder1]
                
                self.log("")
                self.log("=" * 80)
                self.log("COMPARISON COMPLETE!")
                self.log("=" * 80)
                self.log(f"Total unique filenames: {len(results)}")
                self.log(f"Exact name matches: {len(exact_matches)}")
                self.log(f"Missing from folder 2: {len(missing_from_folder2)}")
                self.log(f"Missing from folder 1: {len(missing_from_folder1)}")
                self.log("")
                self.log(f"Text report: {output_filename}")
                self.last_report_html = str(Path(output_filename).with_suffix('.html'))
                self.log(f"HTML report: {self.last_report_html}")
                
            elif mode == "archives":
                self.log("Running Archive Contents Comparison...")
                comparer = ArchiveContentsComparer(folder1, folder2)
                comparer.use_local_copy = use_local
                comparer.max_retries = 5
                comparer.retry_delay = 1.0
                
                results = comparer.compare_archives()
                
                output_filename = f"ascf_archives_report_{timestamp}.txt"
                comparer.save_archive_report(output_filename)
                
                self.log("")
                self.log("=" * 80)
                self.log("COMPARISON COMPLETE!")
                self.log("=" * 80)
                self.log(f"Total archives analyzed: {len(results)}")
                self.log("")
                self.log(f"Text report: {output_filename}")
                self.last_report_html = str(Path(output_filename).with_suffix('.html'))
                self.log(f"HTML report: {self.last_report_html}")
                
            else:  # full
                self.log("Running Full Comparison...")
                comparer = FolderComparer(folder1, folder2)
                comparer.use_local_copy = use_local
                comparer.max_retries = 5
                comparer.retry_delay = 1.0
                comparer.show_missing_only = not show_all
                
                results = comparer.compare_folders()
                
                output_filename = f"ascf_patch_comparison_{timestamp}.txt"
                comparer.save_report(output_filename)
                
                self.log("")
                self.log("=" * 80)
                self.log("COMPARISON COMPLETE!")
                self.log("=" * 80)
                self.log(f"Total files analyzed: {len(results)}")
                self.log("")
                self.log(f"Text report: {output_filename}")
                self.last_report_html = str(Path(output_filename).with_suffix('.html'))
                self.log(f"HTML report: {self.last_report_html}")
            
            self.log("")
            self.log("✅ You can now open the HTML report by clicking 'Open Last Report'")
            self.open_report_button.config(state='normal')
            
        except Exception as e:
            self.log("")
            self.log("=" * 80)
            self.log(f"❌ ERROR: {e}")
            self.log("=" * 80)
            import traceback
            self.log(traceback.format_exc())
        
        finally:
            self.run_button.config(state='normal')

def main():
    root = tk.Tk()
    app = ComparisonGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()











