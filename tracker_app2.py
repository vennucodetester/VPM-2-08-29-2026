
import sys
import os

# Add project root to path so imports work
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Raise Python's recursion limit for large, deeply-nested project files.
# Default is 1000; 5000 gives plenty of headroom without risk.
sys.setrecursionlimit(5000)

from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow
from utils.app_support import append_support_event
from utils import usage_logger
from vpm_tracker_core import AppConstants

def main():
    def log_excepthook(exc_type, exc, tb):
        usage_logger.log("error", where="uncaught", type=exc_type.__name__)
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = log_excepthook
    app = QApplication(sys.argv)

    # Telemetry is required for this build so work-session issues can be reviewed.
    # Force it on at startup in case an older saved QSettings value disabled it.
    usage_logger.set_enabled(True)
    usage_logger.log("app_start", version=AppConstants.VERSION)
    append_support_event("app_start")
    
    # Force Light Mode / Excel-like style
    app.setStyle("Fusion") # Fusion gives us a clean base to style on top of

    # Safe Light Theme
    # We target specific widgets to ensure White/Black contrast without breaking headers
    app.setStyleSheet("""
        QMainWindow, QWidget {
            background-color: #ffffff;
            color: #000000;
        }
        QTreeWidget {
            background-color: #ffffff;
            color: #000000;
            alternate-background-color: #f0f0f0;
            selection-background-color: #e6f2ff;
            selection-color: #000000;
            border: 1px solid #d0d0d0;
        }
        /* Header styling that ensures text is visible */
        QHeaderView::section {
            background-color: #e0e0e0;
            color: #000000;
            border: 1px solid #d0d0d0;
            padding: 4px;
            font-weight: bold;
        }
        QToolBar {
            background-color: #f0f0f0;
            border-bottom: 1px solid #d0d0d0;
        }
        /* Ensure input fields are visible */
        QLineEdit, QDateEdit, QComboBox {
            background-color: #ffffff;
            color: #000000;
            border: 1px solid #ccc;
        }
    """)
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
