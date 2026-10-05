"""Desktop palette and compact, native Qt widget styling."""

STYLE = """
QWidget { font-family: 'Segoe UI'; font-size: 13px; color: #e6edf7; }
QMainWindow, QStackedWidget, QDialog, QWidget#page { background: #111722; }
QFrame#sidebar { background: #0c111b; border-right: 1px solid #263044; }
QFrame#card { background: #192131; border: 1px solid #2a354a; border-radius: 12px; }
QLabel#muted { color: #9baac1; }
QLabel#title { font-size: 25px; font-weight: 650; }
QLabel#brand { font-size: 30px; font-weight: 750; color: #a9befc; }
QLabel#badge { background: #253554; color: #b9ceff; padding: 5px 10px; border-radius: 7px; }
QPushButton { background: #26334b; border: 1px solid #344560; border-radius: 7px;
              padding: 8px 12px; min-height: 20px; }
QPushButton:hover { background: #344866; }
QPushButton:pressed { background: #405879; }
QPushButton:disabled { color: #69788d; background: #1b2535; border-color: #263044; }
QPushButton#primary { background: #436bdb; border-color: #668cf2; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #537ce9; }
QPushButton#primary:disabled { background: #293755; color: #7989a8; border-color: #34415e; }
QPushButton#nav { background: transparent; border: none; text-align: left; padding: 12px; }
QPushButton#nav:checked { background: #243554; color: #c4d5ff; }
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox { background: #0f1725; border: 1px solid #35445f;
              border-radius: 6px; padding: 7px; selection-background-color: #466fdb; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: #7499ff; }
QComboBox QAbstractItemView { background: #172235; selection-background-color: #365bb8; }
QComboBox, QSpinBox { min-height: 18px; }
QTableWidget, QTableView { background: #141d2c; border: 1px solid #2c3950; border-radius: 7px;
               gridline-color: #26344a; selection-background-color: #2c4167; }
QHeaderView::section { background: #202d42; color: #adbed6; border: none; padding: 9px; }
QTableWidget::item { padding: 8px; }
QProgressBar { border: none; background: #253148; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background: #628afa; border-radius: 4px; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QScrollArea { border: none; background: transparent; }
QStatusBar { background: #0c111b; color: #a4b6d0; }
QToolTip { color: #e6edf7; background: #25344d; border: 1px solid #6d88b0; padding: 6px; }
"""
