"""
Campaign Optimization Tool — PyQt5 desktop UI.
Digital Turbine Preload Campaign Optimizer.
Runs optimizer pipeline in a background thread; all logic in optimizer.py.
"""

import os
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QDoubleSpinBox, QFileDialog,
    QProgressBar, QTableWidget, QTableWidgetItem, QGroupBox,
    QMessageBox, QSplitter, QFrame, QGridLayout,
)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QFont, QColor, QPalette

from optimizer import run_optimization, col_letter_to_idx


# --- Background worker ---

class OptimizationWorker(QThread):
    """Runs run_optimization() in a background thread."""
    finished = pyqtSignal(object, dict)  # (output_bytes, summary)
    error = pyqtSignal(str)

    def __init__(self, internal_file, advertiser_file, kpi_col_d7_idx, kpi_col_d2nd_idx, kpi_d7_pct, kpi_d2nd_pct):
        super().__init__()
        self.internal_file = internal_file
        self.advertiser_file = advertiser_file
        self.kpi_col_d7_idx = kpi_col_d7_idx
        self.kpi_col_d2nd_idx = kpi_col_d2nd_idx
        self.kpi_d7_pct = kpi_d7_pct
        self.kpi_d2nd_pct = kpi_d2nd_pct

    def run(self):
        try:
            output_bytes, summary = run_optimization(
                internal_file=self.internal_file,
                advertiser_file=self.advertiser_file,
                kpi_col_d7_idx=self.kpi_col_d7_idx,
                kpi_col_d2nd_idx=self.kpi_col_d2nd_idx,
                kpi_d7_pct=self.kpi_d7_pct,
                kpi_d2nd_pct=self.kpi_d2nd_pct,
            )
            self.finished.emit(output_bytes, summary)
        except Exception as e:
            self.error.emit(str(e))


# --- Main window ---

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.output_bytes = None
        self.internal_path = ""
        self.advertiser_path = ""
        self._build_ui()
        self._apply_styles()

    def _build_ui(self):
        self.setWindowTitle("Campaign Optimization Tool")
        self.setMinimumSize(900, 700)
        self.setStyleSheet("QMainWindow { background-color: #F5F7FA; }")
        font = QFont()
        font.setPointSize(10)
        self.setFont(font)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(16)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # --- 1. Header ---
        title_label = QLabel("Campaign Optimization Tool")
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(16)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: #1F3864;")
        main_layout.addWidget(title_label)

        subtitle_label = QLabel("Digital Turbine Preload Campaign Optimizer")
        subtitle_label.setStyleSheet("color: #666666; font-size: 10pt;")
        main_layout.addWidget(subtitle_label)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        line.setStyleSheet("background-color: #ddd;")
        main_layout.addWidget(line)

        # --- 2. File selection group ---
        file_group = QGroupBox("Input Files")
        file_layout = QVBoxLayout(file_group)
        file_layout.setSpacing(10)

        row1 = QHBoxLayout()
        lbl_internal = QLabel("Internal Campaign Data (.xlsx)")
        lbl_internal.setFixedWidth(220)
        row1.addWidget(lbl_internal)
        self.internal_edit = QLineEdit()
        self.internal_edit.setReadOnly(True)
        self.internal_edit.setPlaceholderText("No file selected")
        row1.addWidget(self.internal_edit)
        btn_internal = QPushButton("Browse...")
        btn_internal.setFixedWidth(90)
        btn_internal.clicked.connect(self._browse_internal)
        row1.addWidget(btn_internal)
        file_layout.addLayout(row1)

        row2 = QHBoxLayout()
        lbl_advertiser = QLabel("Advertiser Performance Report (.csv)")
        lbl_advertiser.setFixedWidth(220)
        row2.addWidget(lbl_advertiser)
        self.advertiser_edit = QLineEdit()
        self.advertiser_edit.setReadOnly(True)
        self.advertiser_edit.setPlaceholderText("No file selected")
        row2.addWidget(self.advertiser_edit)
        btn_advertiser = QPushButton("Browse...")
        btn_advertiser.setFixedWidth(90)
        btn_advertiser.clicked.connect(self._browse_advertiser)
        row2.addWidget(btn_advertiser)
        file_layout.addLayout(row2)

        main_layout.addWidget(file_group)

        # --- 3. KPI settings group ---
        kpi_group = QGroupBox("KPI Settings")
        kpi_layout = QGridLayout(kpi_group)

        kpi_layout.addWidget(QLabel("ROI D7 Column Letter"), 0, 0)
        self.d7_col_edit = QLineEdit()
        self.d7_col_edit.setMaxLength(1)
        self.d7_col_edit.setText("I")
        self.d7_col_edit.setMaximumWidth(60)
        kpi_layout.addWidget(self.d7_col_edit, 0, 1)

        kpi_layout.addWidget(QLabel("ROI D2nd Column Letter (D14 or D30)"), 0, 2)
        self.d2nd_col_edit = QLineEdit()
        self.d2nd_col_edit.setMaxLength(1)
        self.d2nd_col_edit.setText("K")
        self.d2nd_col_edit.setMaximumWidth(60)
        kpi_layout.addWidget(self.d2nd_col_edit, 0, 3)

        kpi_layout.addWidget(QLabel("D7 KPI Target (%)"), 1, 0)
        self.kpi_d7_spin = QDoubleSpinBox()
        self.kpi_d7_spin.setRange(0, 100)
        self.kpi_d7_spin.setDecimals(2)
        self.kpi_d7_spin.setSingleStep(0.01)
        self.kpi_d7_spin.setValue(3.36)
        kpi_layout.addWidget(self.kpi_d7_spin, 1, 1)

        kpi_layout.addWidget(QLabel("D2nd KPI Target (%)"), 1, 2)
        self.kpi_d2nd_spin = QDoubleSpinBox()
        self.kpi_d2nd_spin.setRange(0, 100)
        self.kpi_d2nd_spin.setDecimals(2)
        self.kpi_d2nd_spin.setSingleStep(0.01)
        self.kpi_d2nd_spin.setValue(13.36)
        kpi_layout.addWidget(self.kpi_d2nd_spin, 1, 3)

        main_layout.addWidget(kpi_group)

        # --- 4. Run section ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # indeterminate
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)

        self.run_btn = QPushButton("Run Optimization")
        self.run_btn.setMinimumHeight(44)
        self.run_btn.setCursor(Qt.PointingHandCursor)
        self.run_btn.clicked.connect(self._on_run_clicked)
        main_layout.addWidget(self.run_btn)

        # --- 5. Results section (hidden until first run) ---
        self.results_group = QGroupBox("Results")
        results_layout = QVBoxLayout(self.results_group)
        self.results_group.setVisible(False)

        # Row 1 — metric cards
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(16)
        self.card_total = self._make_metric_card("Total Sites", "0")
        self.card_actioned = self._make_metric_card("Sites Actioned", "0")
        self.card_disregarded = self._make_metric_card("Sites Disregarded", "0")
        self.card_cap = self._make_metric_card("Daily Cap Suggestions", "0")
        self.card_kpi_col = self._make_metric_card("KPI Column Used", "-")
        cards_layout.addWidget(self.card_total)
        cards_layout.addWidget(self.card_actioned)
        cards_layout.addWidget(self.card_disregarded)
        cards_layout.addWidget(self.card_cap)
        cards_layout.addWidget(self.card_kpi_col)
        results_layout.addLayout(cards_layout)

        # Row 2 — two tables
        tables_splitter = QSplitter(Qt.Horizontal)
        self.action_table = QTableWidget()
        self.action_table.setColumnCount(2)
        self.action_table.setHorizontalHeaderLabels(["Action", "Count"])
        self.action_table.horizontalHeader().setStretchLastSection(True)
        self.action_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.action_table.setAlternatingRowColors(True)
        pal = self.action_table.palette()
        pal.setColor(QPalette.AlternateBase, QColor("#F0F4FA"))
        self.action_table.setPalette(pal)
        self.action_table.setFixedHeight(180)
        tables_splitter.addWidget(self.action_table)

        self.segment_table = QTableWidget()
        self.segment_table.setColumnCount(2)
        self.segment_table.setHorizontalHeaderLabels(["Segment", "Count"])
        self.segment_table.horizontalHeader().setStretchLastSection(True)
        self.segment_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.segment_table.setAlternatingRowColors(True)
        pal2 = self.segment_table.palette()
        pal2.setColor(QPalette.AlternateBase, QColor("#F0F4FA"))
        self.segment_table.setPalette(pal2)
        self.segment_table.setFixedHeight(180)
        tables_splitter.addWidget(self.segment_table)
        results_layout.addWidget(tables_splitter)

        # Row 3 — save button
        self.save_btn = QPushButton("Save Optimization Report")
        self.save_btn.setStyleSheet("""
            QPushButton { background-color: #217346; color: white; font-weight: bold; padding: 8px 16px; }
            QPushButton:hover { background-color: #1e6639; }
        """)
        self.save_btn.clicked.connect(self._save_report)
        results_layout.addWidget(self.save_btn)

        main_layout.addWidget(self.results_group)
        main_layout.addStretch()

    def _make_metric_card(self, description, value_text):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet("QFrame { background-color: white; border: 1px solid #E0E0E0; border-radius: 4px; padding: 8px; }")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        value_label = QLabel(value_text)
        value_label.setObjectName("value")
        value_font = QFont()
        value_font.setPointSize(18)
        value_font.setBold(True)
        value_label.setFont(value_font)
        value_label.setStyleSheet("color: #1F3864;")
        desc_label = QLabel(description)
        desc_label.setStyleSheet("color: #666666; font-size: 9pt;")
        layout.addWidget(value_label)
        layout.addWidget(desc_label)
        frame.value_label = value_label
        frame.desc_label = desc_label
        return frame

    def _apply_styles(self):
        self.run_btn.setStyleSheet("""
            QPushButton {
                background-color: #1F3864;
                color: white;
                font-weight: bold;
                font-size: 12pt;
            }
            QPushButton:hover { background-color: #2E4D8A; }
            QPushButton:disabled { background-color: #9CA3AF; color: #E5E7EB; }
        """)

    def _browse_internal(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Internal Campaign Data", "", "Excel files (*.xlsx)")
        if path:
            self.internal_path = path
            self.internal_edit.setText(path)

    def _browse_advertiser(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Advertiser Performance Report", "", "CSV files (*.csv)")
        if path:
            self.advertiser_path = path
            self.advertiser_edit.setText(path)

    def _validate(self):
        if not self.internal_path or not self.internal_path.strip():
            QMessageBox.warning(self, "Validation", "Please select the Internal Campaign Data (.xlsx) file.")
            return False
        if not os.path.isfile(self.internal_path):
            QMessageBox.warning(self, "Validation", "Internal Campaign Data file does not exist.")
            return False
        if not self.advertiser_path or not self.advertiser_path.strip():
            QMessageBox.warning(self, "Validation", "Please select the Advertiser Performance Report (.csv) file.")
            return False
        if not os.path.isfile(self.advertiser_path):
            QMessageBox.warning(self, "Validation", "Advertiser Performance Report file does not exist.")
            return False
        d7 = self.d7_col_edit.text().strip().upper()
        if len(d7) != 1 or not d7.isalpha():
            QMessageBox.warning(self, "Validation", "ROI D7 Column Letter must be a single letter A–Z.")
            return False
        d2nd = self.d2nd_col_edit.text().strip().upper()
        if len(d2nd) != 1 or not d2nd.isalpha():
            QMessageBox.warning(self, "Validation", "ROI D2nd Column Letter must be a single letter A–Z.")
            return False
        if self.kpi_d7_spin.value() <= 0:
            QMessageBox.warning(self, "Validation", "D7 KPI Target must be greater than 0.")
            return False
        if self.kpi_d2nd_spin.value() <= 0:
            QMessageBox.warning(self, "Validation", "D2nd KPI Target must be greater than 0.")
            return False
        return True

    def _on_run_clicked(self):
        if not self._validate():
            return
        self.run_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        d7_letter = self.d7_col_edit.text().strip().upper()
        d2nd_letter = self.d2nd_col_edit.text().strip().upper()
        try:
            kpi_col_d7_idx = col_letter_to_idx(d7_letter)
            kpi_col_d2nd_idx = col_letter_to_idx(d2nd_letter)
        except Exception as e:
            self.progress_bar.setVisible(False)
            self.run_btn.setEnabled(True)
            QMessageBox.critical(self, "Error", f"Invalid column letter: {e}")
            return
        self.worker = OptimizationWorker(
            internal_file=self.internal_path,
            advertiser_file=self.advertiser_path,
            kpi_col_d7_idx=kpi_col_d7_idx,
            kpi_col_d2nd_idx=kpi_col_d2nd_idx,
            kpi_d7_pct=self.kpi_d7_spin.value(),
            kpi_d2nd_pct=self.kpi_d2nd_spin.value(),
        )
        self.worker.finished.connect(self._on_success)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _on_success(self, output_bytes, summary):
        self.output_bytes = output_bytes
        self.progress_bar.setVisible(False)
        self.run_btn.setEnabled(True)
        self._populate_results(summary)
        self.results_group.setVisible(True)
        actioned = summary.get("rows_actioned", 0)
        QMessageBox.information(self, "Complete", f"Optimization complete! {actioned} sites actioned.")

    def _on_error(self, message):
        self.progress_bar.setVisible(False)
        self.run_btn.setEnabled(True)
        QMessageBox.critical(self, "Error", message)

    def _populate_results(self, summary):
        self.card_total.value_label.setText(str(summary.get("total_rows", 0)))
        self.card_actioned.value_label.setText(str(summary.get("rows_actioned", 0)))
        self.card_disregarded.value_label.setText(str(summary.get("rows_disregarded", 0)))
        self.card_cap.value_label.setText(str(summary.get("rows_with_cap", 0)))
        self.card_kpi_col.value_label.setText(str(summary.get("roi_d2nd_col", "-")))

        action_breakdown = summary.get("action_breakdown") or {}
        items = sorted(action_breakdown.items(), key=lambda x: -x[1])
        self.action_table.setRowCount(len(items))
        for i, (action, count) in enumerate(items):
            self.action_table.setItem(i, 0, QTableWidgetItem(str(action)))
            self.action_table.setItem(i, 1, QTableWidgetItem(str(count)))

        segment_breakdown = summary.get("segment_breakdown") or {}
        items = sorted(segment_breakdown.items(), key=lambda x: -x[1])
        self.segment_table.setRowCount(len(items))
        for i, (segment, count) in enumerate(items):
            self.segment_table.setItem(i, 0, QTableWidgetItem(str(segment)))
            self.segment_table.setItem(i, 1, QTableWidgetItem(str(count)))

    def _save_report(self):
        if not self.output_bytes:
            QMessageBox.warning(self, "Save", "No optimization output to save. Run optimization first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Optimization Report", "optimization_output.xlsx", "Excel files (*.xlsx)")
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(self.output_bytes.getvalue())
            QMessageBox.information(self, "Saved", f"Report saved to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not save file: {e}")


def main():
    import sys
    # Ensure we can show errors in a message box if Qt is available
    try:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        win = MainWindow()
        win.show()
        sys.exit(app.exec_())
    except Exception as e:
        try:
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "Campaign Optimization Tool - Error", str(e))
            sys.exit(1)
        except Exception:
            print("Error:", e)
            input("Press Enter to close...")
            sys.exit(1)


if __name__ == "__main__":
    import sys
    try:
        main()
    except Exception as e:
        try:
            from PyQt5.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "Campaign Optimization Tool - Error", str(e))
            sys.exit(1)
        except Exception:
            print("Error:", e)
            input("Press Enter to close...")
            sys.exit(1)
