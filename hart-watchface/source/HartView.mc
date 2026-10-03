import Toybox.Graphics;
import Toybox.Lang;
import Toybox.Math;
import Toybox.System;
import Toybox.Time;
import Toybox.Time.Gregorian;
import Toybox.WatchUi;

//! The hart web UI's colours (src/hart/server/web/static/app.css)
module Palette {
    const BG = 0x0a140f;
    const SOFT = 0x1a2f23;
    const LINE = 0x2a4a38;
    const FAINT = 0x5b7564;
    const MUTED = 0x8ba393;
    const TEXT = 0xd6d3d1;
    const CREAM = 0xf2e8d5;
    const GREEN = 0x6eb886;
    const AMBER = 0xe2a45f;
    const RED = 0xe5604a;
    const SUNSET = 0xe27d60;
    const AOD_OUTLINE = 0x9a917f;
}

//! Layout is designed on the FR965's 454px screen; y values are text baselines.
class HartView extends WatchUi.WatchFace {
    private const DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
    //! Date baseline sits this far above the time baseline in both modes
    private const DATE_ABOVE_TIME = 128;
    private const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

    private var _awake as Boolean = true;
    private var _scale as Float = 1.0;
    private var _time as FontResource;
    private var _label as FontResource;
    private var _body as FontResource;
    private var _small as FontResource;
    private var _arc as FontResource;
    private var _italic as FontResource;

    function initialize() {
        WatchFace.initialize();
        _time = WatchUi.loadResource(Rez.Fonts.Time) as FontResource;
        _label = WatchUi.loadResource(Rez.Fonts.Label) as FontResource;
        _body = WatchUi.loadResource(Rez.Fonts.Body) as FontResource;
        _small = WatchUi.loadResource(Rez.Fonts.Small) as FontResource;
        _arc = WatchUi.loadResource(Rez.Fonts.Arc) as FontResource;
        _italic = WatchUi.loadResource(Rez.Fonts.SerifItalic) as FontResource;
    }

    function onLayout(dc as Dc) as Void {
        _scale = dc.getWidth() / 454.0;
    }

    function onUpdate(dc as Dc) as Void {
        if (dc has :setAntiAlias) {
            dc.setAntiAlias(true);
        }
        var clock = System.getClockTime();
        if (_awake || !System.getDeviceSettings().requiresBurnInProtection) {
            drawActive(dc, clock);
        } else {
            drawAlwaysOn(dc, clock);
        }
    }

    function onEnterSleep() as Void {
        _awake = false;
        WatchUi.requestUpdate();
    }

    function onExitSleep() as Void {
        _awake = true;
        WatchUi.requestUpdate();
    }

    private function drawActive(dc as Dc, clock as System.ClockTime) as Void {
        var cx = dc.getWidth() / 2;
        dc.setColor(Palette.BG, Palette.BG);
        dc.clear();

        var bodyBattery = Data.bodyBattery();
        var battery = Data.battery();
        drawGauge(dc, true, bodyBattery, Palette.GREEN);
        drawGauge(dc, false, battery, Palette.AMBER);
        text(dc, px(46), py(236), _arc, valueOrDash(bodyBattery), Palette.GREEN, Graphics.TEXT_JUSTIFY_LEFT);
        text(dc, px(408), py(236), _arc, battery.toString(), Palette.AMBER, Graphics.TEXT_JUSTIFY_RIGHT);

        text(dc, cx, py(246 - DATE_ABOVE_TIME), _label, dateLabel(), Palette.MUTED, Graphics.TEXT_JUSTIFY_CENTER);
        text(dc, cx, py(246), _time, timeLabel(clock), Palette.CREAM, Graphics.TEXT_JUSTIFY_CENTER);

        dc.setColor(Palette.LINE, Graphics.COLOR_TRANSPARENT);
        dc.setPenWidth(px(2));
        dc.drawLine(px(187), py(276), px(267), py(276));

        drawVitals(dc, cx, py(320));
        drawCountdown(dc, cx, py(372));
    }

    //! Burn-in safe: outlined digits, a few lit pixels, position drifting each minute.
    private function drawAlwaysOn(dc as Dc, clock as System.ClockTime) as Void {
        var cx = dc.getWidth() / 2;
        var drift = [-6, -3, 0, 3, 6] as Array<Number>;
        var dx = px(drift[clock.min % 5]);
        var dy = px(drift[(clock.min / 5) % 5]);
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();

        text(dc, cx + dx, py(262 - DATE_ABOVE_TIME) + dy, _label, dateLabel(), Palette.FAINT, Graphics.TEXT_JUSTIFY_CENTER);

        var time = timeLabel(clock);
        var o = px(2);
        var offsets = [[-o, 0], [o, 0], [0, -o], [0, o], [-o, -o], [o, -o], [-o, o], [o, o]] as Array<Array<Number>>;
        for (var i = 0; i < offsets.size(); i++) {
            text(dc, cx + dx + offsets[i][0], py(262) + dy + offsets[i][1], _time, time, Palette.AOD_OUTLINE, Graphics.TEXT_JUSTIFY_CENTER);
        }
        text(dc, cx + dx, py(262) + dy, _time, time, Graphics.COLOR_BLACK, Graphics.TEXT_JUSTIFY_CENTER);
    }

    //! A 100° arc on the left (filling upward clockwise) or right (upward anticlockwise)
    private function drawGauge(dc as Dc, left as Boolean, percent as Number?, color as Number) as Void {
        var cx = dc.getWidth() / 2;
        var cy = dc.getHeight() / 2;
        var r = px(212);
        var pen = px(7);
        var start = left ? 230 : 310;
        var direction = left ? Graphics.ARC_CLOCKWISE : Graphics.ARC_COUNTER_CLOCKWISE;
        var sign = left ? -1 : 1;
        dc.setPenWidth(pen);

        dc.setColor(Palette.SOFT, Graphics.COLOR_TRANSPARENT);
        dc.drawArc(cx, cy, r, direction, start, start + sign * 100);
        cap(dc, cx, cy, r, start, pen);
        cap(dc, cx, cy, r, start + sign * 100, pen);

        if (percent == null || percent <= 0) {
            return;
        }
        var sweep = (percent > 100 ? 100 : percent);
        dc.setColor(color, Graphics.COLOR_TRANSPARENT);
        if (sweep > 1) {
            dc.drawArc(cx, cy, r, direction, start, start + sign * sweep);
        }
        cap(dc, cx, cy, r, start, pen);
        cap(dc, cx, cy, r, start + sign * sweep, pen);
    }

    //! Round line end at an arc angle (degrees anticlockwise from 3 o'clock)
    private function cap(dc as Dc, cx as Number, cy as Number, r as Number, degrees as Number, pen as Number) as Void {
        var a = Math.toRadians(degrees);
        dc.fillCircle(cx + r * Math.cos(a), cy - r * Math.sin(a), pen / 2.0);
    }

    //! Heart, bpm · steps
    private function drawVitals(dc as Dc, cx as Number, baseline as Number) as Void {
        var heart = px(20);
        var gap = px(8);
        var hr = valueOrDash(Data.heartRate());
        var steps = groupThousands(Data.steps());
        var sep = "   ·   ";
        var unit = " steps";
        var width = heart + gap
            + dc.getTextWidthInPixels(hr, _body)
            + dc.getTextWidthInPixels(sep, _body)
            + dc.getTextWidthInPixels(steps, _body)
            + dc.getTextWidthInPixels(unit, _small);
        var x = cx - width / 2;
        drawHeart(dc, x, baseline, heart);
        x += heart + gap;
        x = text(dc, x, baseline, _body, hr, Palette.TEXT, Graphics.TEXT_JUSTIFY_LEFT);
        x = text(dc, x, baseline, _body, sep, Palette.FAINT, Graphics.TEXT_JUSTIFY_LEFT);
        x = text(dc, x, baseline, _body, steps, Palette.TEXT, Graphics.TEXT_JUSTIFY_LEFT);
        text(dc, x, baseline, _small, unit, Palette.FAINT, Graphics.TEXT_JUSTIFY_LEFT);
    }

    //! "323 days to go", hidden when no race date is set or it has passed
    private function drawCountdown(dc as Dc, cx as Number, baseline as Number) as Void {
        var days = Data.daysToRace();
        if (days == null || days < 0) {
            return;
        }
        if (days == 0) {
            text(dc, cx, baseline, _italic, "Race day", Palette.SUNSET, Graphics.TEXT_JUSTIFY_CENTER);
            return;
        }
        var count = days.toString();
        var rest = days == 1 ? " day to go" : " days to go";
        var x = cx - (dc.getTextWidthInPixels(count, _italic) + dc.getTextWidthInPixels(rest, _italic)) / 2;
        x = text(dc, x, baseline, _italic, count, Palette.SUNSET, Graphics.TEXT_JUSTIFY_LEFT);
        text(dc, x, baseline, _italic, rest, Palette.MUTED, Graphics.TEXT_JUSTIFY_LEFT);
    }

    private function drawHeart(dc as Dc, x as Number, baseline as Number, size as Number) as Void {
        var r = size / 4.0;
        var top = baseline - size * 0.85;
        dc.setColor(Palette.RED, Graphics.COLOR_TRANSPARENT);
        dc.fillCircle(x + r, top + r, r);
        dc.fillCircle(x + size - r, top + r, r);
        dc.fillPolygon([
            [x, top + r * 1.3],
            [x + size, top + r * 1.3],
            [x + size / 2, baseline - size * 0.05],
        ] as Array<[Numeric, Numeric]>);
    }

    //! Draw text with its baseline at y; returns the x just past it
    private function text(dc as Dc, x as Number, baseline as Number, font as FontResource, value as String, color as Number, justify as Number) as Number {
        dc.setColor(color, Graphics.COLOR_TRANSPARENT);
        dc.drawText(x, baseline - Graphics.getFontAscent(font), font, value, justify);
        return x + dc.getTextWidthInPixels(value, font);
    }

    private function dateLabel() as String {
        var now = Gregorian.info(Time.now(), Time.FORMAT_SHORT);
        return DAYS[(now.day_of_week as Number) - 1] + " " + now.day + " " + MONTHS[(now.month as Number) - 1];
    }

    private function timeLabel(clock as System.ClockTime) as String {
        var hour = clock.hour;
        if (!System.getDeviceSettings().is24Hour) {
            hour = hour % 12 == 0 ? 12 : hour % 12;
            return hour + ":" + clock.min.format("%02d");
        }
        return hour.format("%02d") + ":" + clock.min.format("%02d");
    }

    private function valueOrDash(value as Number?) as String {
        return value == null ? "--" : value.toString();
    }

    //! 4210 → "4 210"
    private function groupThousands(value as Number?) as String {
        if (value == null) {
            return "--";
        }
        var digits = value.toString();
        var out = "";
        var n = digits.length();
        for (var i = 0; i < n; i++) {
            if (i > 0 && (n - i) % 3 == 0) {
                out += " ";
            }
            out += digits.substring(i, i + 1);
        }
        return out;
    }

    private function px(v as Number) as Number {
        return (v * _scale).toNumber();
    }

    private function py(v as Number) as Number {
        return (v * _scale).toNumber();
    }
}
