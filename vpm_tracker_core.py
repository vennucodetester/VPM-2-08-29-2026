
from enum import Enum
from PyQt6.QtGui import QColor
from utils.version_info import detect_version

class AppConstants:
    APP_NAME = "VPM Tracker"
    VERSION = detect_version()
    REVISION_LABEL = f"Rev {VERSION}"
    FILE_EXT = ".vpmt"

class Columns:
    TREE = 0
    START = 1
    END = 2
    DURATION = 3
    POTENTIAL = 4
    REALIZED = 5
    STATUS = 6
    OWNER = 7
    PREDECESSOR = 8
    NOTES = 9
    DELAY = 10
    COUNT = 11

    NAMES = [
        "Task Name", "Start", "End", "Duration",
        "Potential $", "Realized $",
        "Status", "Waiting On", "Depends On", "Notes", "Delay",
    ]

class Status(Enum):
    PENDING = "Not Started"
    IN_PROGRESS = "In Progress"
    COMPLETED = "Completed"
    OVERDUE = "Overdue" # Calculated, not always stored explicitly as a status enum, but useful for logic

class Colors:
    # Backgrounds
    BG_DARK = QColor("#ffffff")      # White background
    BG_TREE = QColor("#f0f0f0")      # Light gray for tree/alternating
    
    # Status Colors (for text or small indicators)
    # Status Colors (for text or small indicators)
    RED = QColor("#FF0000")          # Pure Bright Red (Overdue)
    GREEN = QColor("#00C853")        # Vivid Green (Completed)
    GRAY = QColor("#212121")         # Almost Black (Parent)
    
    # UI
    SELECTION = QColor("#0078d7")    # Standard Windows Blue
    TEXT_WHITE = QColor("#000000")   # Black text
    
    # Gantt / Dark Theme
    BG_DARK = QColor("#1e1e1e")
    GANTT_GRID = QColor("#333333")
    GANTT_TEXT = QColor("#dddddd")
    BAR_BLUE = QColor("#2979FF")     # In Progress (Normal)
    BAR_GREEN = QColor("#00C853")    # Completed
    BAR_DELAYED = QColor("#D50000")  # Red (Delayed)
    BAR_CRITICAL = QColor("#FF4081") # Pink (Critical)
    
    # Parent Bars
    BAR_PARENT_COLLAPSED = QColor("#424242") # (Fallback, usually overridden by status)
    BAR_PARENT_EXPANDED = QColor("#666666")  # Solid Gray
