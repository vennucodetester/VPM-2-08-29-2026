from datetime import datetime, timedelta
from utils.config_manager import ConfigManager

DATE_FMT = "%Y-%m-%d"

class WorkdayCalculator:
    @staticmethod
    def add_workdays(start_date_str: str, days: int, holidays=None, exclude_weekends=None) -> str:
        """
        Return the date `days` workdays from start_date (inclusive of start).
        Duration of 1 means End == Start. Invalid input returns the original string
        unchanged and logs a warning; callers must still validate if they need to.
        """
        if not start_date_str:
            return start_date_str

        if holidays is None or exclude_weekends is None:
            config = ConfigManager()
            if holidays is None:
                holidays = set(config.get_holidays())
            else:
                holidays = set(holidays)
            if exclude_weekends is None:
                exclude_weekends = config.get_exclude_weekends()
            else:
                exclude_weekends = bool(exclude_weekends)
        else:
            holidays = set(holidays)
            exclude_weekends = bool(exclude_weekends)

        try:
            current_date = datetime.strptime(start_date_str, DATE_FMT)
        except ValueError:
            import logging
            logging.warning("add_workdays: invalid start_date %r", start_date_str)
            return start_date_str

        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 1
        if days < 1:
            days = 1

        # Hard cap to prevent runaway loops on absurd inputs.
        target = min(days - 1, 10_000)
        added = 0
        while added < target:
            current_date += timedelta(days=1)
            if WorkdayCalculator.is_workday(current_date, holidays, exclude_weekends):
                added += 1

        return current_date.strftime(DATE_FMT)

    @staticmethod
    def get_next_workday(date_str: str, holidays=None, exclude_weekends=None) -> str:
        """
        Get the next valid workday after the given date.
        """
        if holidays is None or exclude_weekends is None:
            config = ConfigManager()
            if holidays is None:
                holidays = set(config.get_holidays())
            else:
                holidays = set(holidays)
            if exclude_weekends is None:
                exclude_weekends = config.get_exclude_weekends()
            else:
                exclude_weekends = bool(exclude_weekends)
        else:
            holidays = set(holidays)
            exclude_weekends = bool(exclude_weekends)
        
        try:
            current_date = datetime.strptime(date_str, DATE_FMT)
        except ValueError:
            return date_str
            
        while True:
            current_date += timedelta(days=1)
            if WorkdayCalculator.is_workday(current_date, holidays, exclude_weekends):
                return current_date.strftime(DATE_FMT)

    @staticmethod
    def calculate_duration(start_date_str: str, end_date_str: str, holidays=None, exclude_weekends=None) -> int:
        """
        Calculate number of workdays between start and end (inclusive).
        """
        if holidays is None or exclude_weekends is None:
            config = ConfigManager()
            if holidays is None:
                holidays = set(config.get_holidays())
            else:
                holidays = set(holidays)
            if exclude_weekends is None:
                exclude_weekends = config.get_exclude_weekends()
            else:
                exclude_weekends = bool(exclude_weekends)
        else:
            holidays = set(holidays)
            exclude_weekends = bool(exclude_weekends)
        
        try:
            start = datetime.strptime(start_date_str, DATE_FMT)
            end = datetime.strptime(end_date_str, DATE_FMT)
        except ValueError:
            return 0
            
        if start > end:
            return 0
            
        count = 0
        curr = start
        while curr <= end:
            if WorkdayCalculator.is_workday(curr, holidays, exclude_weekends):
                count += 1
            curr += timedelta(days=1)
            
        return count

    @staticmethod
    def is_workday(date_obj: datetime, holidays: set, exclude_weekends: bool) -> bool:
        date_str = date_obj.strftime(DATE_FMT)
        
        if date_str in holidays:
            return False
            
        if exclude_weekends:
            # 0=Mon, 6=Sun
            if date_obj.weekday() >= 5:
                return False
                
        return True
