import Toybox.Activity;
import Toybox.ActivityMonitor;
import Toybox.Application;
import Toybox.Lang;
import Toybox.SensorHistory;
import Toybox.System;
import Toybox.Time;
import Toybox.Time.Gregorian;

//! Readings the face shows, each null when the watch has nothing to report.
module Data {
    function heartRate() as Number? {
        var info = Activity.getActivityInfo();
        if (info != null && info.currentHeartRate != null) {
            return info.currentHeartRate;
        }
        var sample = ActivityMonitor.getHeartRateHistory(1, true).next();
        if (sample != null && sample.heartRate != ActivityMonitor.INVALID_HR_SAMPLE) {
            return sample.heartRate;
        }
        return null;
    }

    function steps() as Number? {
        return ActivityMonitor.getInfo().steps;
    }

    //! Body Battery as 0..100: the newest sample that has a value. The newest entry is
    //! sometimes empty while the watch is still computing it, which made the number flicker.
    function bodyBattery() as Number? {
        if (!(Toybox has :SensorHistory) || !(SensorHistory has :getBodyBatteryHistory)) {
            return null;
        }
        var history = SensorHistory.getBodyBatteryHistory({
            :period => 10,
            :order => SensorHistory.ORDER_NEWEST_FIRST,
        });
        for (var sample = history.next(); sample != null; sample = history.next()) {
            if (sample.data != null) {
                return (sample.data as Numeric).toNumber();
            }
        }
        return null;
    }

    //! Watch battery as 0..100
    function battery() as Number {
        return System.getSystemStats().battery.toNumber();
    }

    //! Days from today to hart's next race, or null when there's no hart data or no race
    function daysToRace(hart as Dictionary?) as Number? {
        var race = hart != null ? parseDate(hart["race"] as String?) : null;
        if (race == null) {
            return null;
        }
        var today = Gregorian.info(Time.now(), Time.FORMAT_SHORT);
        return race - dayNumber(today.year, today.month as Number, today.day);
    }

    //! Today as "YYYY-MM-DD"
    function todayIso() as String {
        var t = Gregorian.info(Time.now(), Time.FORMAT_SHORT);
        return t.year.format("%04d") + "-" + (t.month as Number).format("%02d") + "-" + t.day.format("%02d");
    }

    //! "YYYY-MM-DD" to a day number, or null
    function parseDate(text as String?) as Number? {
        if (text == null || text.length() != 10) {
            return null;
        }
        var year = (text.substring(0, 4) as String).toNumber();
        var month = (text.substring(5, 7) as String).toNumber();
        var day = (text.substring(8, 10) as String).toNumber();
        if (year == null || month == null || day == null || month < 1 || month > 12 || day < 1 || day > 31) {
            return null;
        }
        return dayNumber(year, month, day);
    }

    //! Days since 1970-01-01 for a civil date (Howard Hinnant's days_from_civil)
    function dayNumber(year as Number, month as Number, day as Number) as Number {
        var y = month <= 2 ? year - 1 : year;
        var era = (y >= 0 ? y : y - 399) / 400;
        var yoe = y - era * 400;
        var mp = (month + 9) % 12;
        var doy = (153 * mp + 2) / 5 + day - 1;
        var doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        return era * 146097 + doe - 719468;
    }
}
