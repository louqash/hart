import Toybox.Application;
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
    //! Date baseline sits this far above the time baseline when there's no hart line between them
    private const DATE_ABOVE_TIME = 128;
    private const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

    //! hart's readiness level → word and colour
    private const READINESS = {
        "green" => ["Ready", Palette.GREEN],
        "amber" => ["Caution", Palette.AMBER],
        "red" => ["Recover", Palette.RED],
        "unknown" => ["No readiness", Palette.FAINT],
    };

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

    //! Without hart data the face keeps the phase 1 layout; with it, a readiness line goes
    //! above the time and form joins the countdown at the bottom.
    private function drawActive(dc as Dc, clock as System.ClockTime) as Void {
        var cx = dc.getWidth() / 2;
        dc.setColor(Palette.BG, Palette.BG);
        dc.clear();

        var bodyBattery = Data.bodyBattery();
        var battery = Data.battery();
        drawGauge(dc, true, bodyBattery, Palette.GREEN);
        drawGauge(dc, false, battery, Palette.AMBER);
        // Values sit by the arcs' top ends: at mid-height a four-digit 24-hour time runs into them
        text(dc, px(93), py(106), _arc, valueOrDash(bodyBattery), Palette.GREEN, Graphics.TEXT_JUSTIFY_CENTER);
        text(dc, px(361), py(106), _arc, battery.toString(), Palette.AMBER, Graphics.TEXT_JUSTIFY_CENTER);

        var hart = Application.Storage.getValue("hart") as Dictionary?;
        var days = Data.daysToRace(hart);
        var time = timeBaseline(hart);
        text(dc, cx, py(dateBaseline(hart)), _label, dateLabel(), Palette.MUTED, Graphics.TEXT_JUSTIFY_CENTER);
        text(dc, cx, py(time), _time, timeLabel(clock), Palette.CREAM, Graphics.TEXT_JUSTIFY_CENTER);

        dc.setColor(Palette.LINE, Graphics.COLOR_TRANSPARENT);
        dc.setPenWidth(px(2));
        dc.drawLine(px(187), py(time + 30), px(267), py(time + 30));

        if (hart == null) {
            drawVitals(dc, cx, py(320));
            drawCountdown(dc, cx, py(372), days);
            drawFetchProblem(dc, cx, py(372));
            return;
        }
        var fresh = isFresh(hart);
        drawStatus(dc, cx, py(146), hart, fresh);
        drawVitals(dc, cx, py(332));
        drawFormAndDays(dc, cx, py(368), hart["form"] as Number?, days, fresh);
        dc.setColor(fresh ? Palette.GREEN : Palette.FAINT, Graphics.COLOR_TRANSPARENT);
        dc.fillCircle(cx, py(402), px(4));
    }

    //! Shared by the active and always-on faces so the time and date don't move between them
    private function timeBaseline(hart as Dictionary?) as Number {
        return hart == null ? 246 : 262;
    }

    private function dateBaseline(hart as Dictionary?) as Number {
        return hart == null ? 246 - DATE_ABOVE_TIME : 104;
    }

    //! Values are current when they are today's and arrived within the last three hours
    private function isFresh(hart as Dictionary) as Boolean {
        var at = Application.Storage.getValue("hartAt") as Number?;
        return at != null && Time.now().value() - at < 3 * 3600 && Data.todayIso().equals(hart["date"] as String?);
    }

    //! Until hart data arrives: "hart: waiting" before the first reply, else the last error code
    //! (see README). Shown only when an address is set.
    private function drawFetchProblem(dc as Dc, cx as Number, baseline as Number) as Void {
        if (Config.hartUrl().length() == 0) {
            return;
        }
        var code = Application.Storage.getValue("hartError") as Number?;
        var label = code == null ? "hart: waiting" : "hart: error " + code;
        text(dc, cx, baseline, _small, label, Palette.FAINT, Graphics.TEXT_JUSTIFY_CENTER);
    }

    //! "Ready · build phase", dimmed when the values are stale; the phase gives way to the last
    //! error code when stale values are all there is
    private function drawStatus(dc as Dc, cx as Number, baseline as Number, hart as Dictionary, fresh as Boolean) as Void {
        var ready = READINESS[hart["ready"]] as [String, Number]?;
        if (ready == null) {
            ready = READINESS["unknown"] as [String, Number];
        }
        var word = ready[0];
        var phase = hart["phase"] as String?;
        var rest = phase == null ? "" : "  ·  " + phase + " phase";
        var code = Application.Storage.getValue("hartError") as Number?;
        if (!fresh && code != null) {
            rest = "  ·  error " + code;
        }
        var x = cx - (dc.getTextWidthInPixels(word, _small) + dc.getTextWidthInPixels(rest, _small)) / 2;
        x = text(dc, x, baseline, _small, word, fresh ? ready[1] : Palette.FAINT, Graphics.TEXT_JUSTIFY_LEFT);
        text(dc, x, baseline, _small, rest, Palette.FAINT, Graphics.TEXT_JUSTIFY_LEFT);
    }

    //! "form +6 · 323 days"; either half is left out when unknown
    private function drawFormAndDays(dc as Dc, cx as Number, baseline as Number, form as Number?, days as Number?, fresh as Boolean) as Void {
        var parts = [] as Array<[String, FontResource, Number]>;
        if (form != null) {
            parts.add(["form ", _small, Palette.MUTED]);
            parts.add([(form > 0 ? "+" : "") + form, _small, fresh ? Palette.TEXT : Palette.FAINT]);
        }
        if (days != null && days >= 0) {
            if (parts.size() > 0) {
                parts.add(["   ·   ", _small, Palette.FAINT]);
            }
            parts.add([days == 0 ? "Race day" : days + (days == 1 ? " day" : " days"), _italic, Palette.SUNSET]);
        }
        var width = 0;
        for (var i = 0; i < parts.size(); i++) {
            width += dc.getTextWidthInPixels(parts[i][0], parts[i][1]);
        }
        var x = cx - width / 2;
        for (var i = 0; i < parts.size(); i++) {
            x = text(dc, x, baseline, parts[i][1], parts[i][0], parts[i][2], Graphics.TEXT_JUSTIFY_LEFT);
        }
    }

    //! Burn-in safe: outlined digits, a few lit pixels, position drifting each minute.
    private function drawAlwaysOn(dc as Dc, clock as System.ClockTime) as Void {
        var cx = dc.getWidth() / 2;
        // Same place as the active face, give or take a 2px drift: enough that the 2px outline
        // never lights the same pixels for long, too little to see the face jump on wake/sleep.
        var drift = [0, 2, 0, -2] as Array<Number>;
        var dx = drift[clock.min % 4];
        var dy = drift[(clock.min / 4 + 1) % 4];
        var hart = Application.Storage.getValue("hart") as Dictionary?;
        var baseline = py(timeBaseline(hart)) + dy;
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_BLACK);
        dc.clear();

        text(dc, cx + dx, py(dateBaseline(hart)) + dy, _label, dateLabel(), Palette.FAINT, Graphics.TEXT_JUSTIFY_CENTER);

        var time = timeLabel(clock);
        var o = px(2);
        var offsets = [[-o, 0], [o, 0], [0, -o], [0, o], [-o, -o], [o, -o], [-o, o], [o, o]] as Array<Array<Number>>;
        for (var i = 0; i < offsets.size(); i++) {
            text(dc, cx + dx + offsets[i][0], baseline + offsets[i][1], _time, time, Palette.AOD_OUTLINE, Graphics.TEXT_JUSTIFY_CENTER);
        }
        text(dc, cx + dx, baseline, _time, time, Graphics.COLOR_BLACK, Graphics.TEXT_JUSTIFY_CENTER);
    }

    //! A 100° arc on the left (filling upward clockwise) or right (upward anticlockwise)
    private function drawGauge(dc as Dc, left as Boolean, percent as Number?, color as Number) as Void {
        var start = left ? 230 : 310;
        var sign = left ? -1 : 1;
        stroke(dc, start, start + sign * 100, Palette.SOFT);
        if (percent != null && percent > 0) {
            stroke(dc, start, start + sign * (percent > 100 ? 100 : percent), color);
        }
    }

    //! A round-ended arc between two angles (degrees anticlockwise from 3 o'clock), filled as a
    //! single polygon — outer edge, rounded end, inner edge, rounded start — so the line and its
    //! ends are one shape. (drawArc plus fillCircle caps never matched in width or position.)
    //! At most 26 + 26 edge points and 5 + 5 cap points: under fillPolygon's 64-point limit.
    private function stroke(dc as Dc, from as Number, to as Number, color as Number) as Void {
        var cx = dc.getWidth() / 2.0;
        var cy = dc.getHeight() / 2.0;
        var r = 212 * _scale;
        var w = 3.5 * _scale;
        var dir = to >= from ? 1 : -1;
        var n = ((to - from).abs() / 4.0).toNumber() + 2;
        var pts = [] as Array<[Numeric, Numeric]>;
        for (var i = 0; i < n; i++) {
            edgePoint(pts, cx, cy, r + w, from + (to - from) * i / (n - 1.0));
        }
        roundEnd(pts, cx, cy, r, w, to, dir, 1);
        for (var i = n - 1; i >= 0; i--) {
            edgePoint(pts, cx, cy, r - w, from + (to - from) * i / (n - 1.0));
        }
        roundEnd(pts, cx, cy, r, w, from, dir, -1);
        dc.setColor(color, Graphics.COLOR_TRANSPARENT);
        dc.fillPolygon(pts);
    }

    private function edgePoint(pts as Array<[Numeric, Numeric]>, cx as Float, cy as Float, radius as Float, degrees as Float) as Void {
        var a = Math.toRadians(degrees);
        pts.add([cx + radius * Math.cos(a), cy - radius * Math.sin(a)]);
    }

    //! Half circle around the arc's end at `degrees`, from the outer edge to the inner (side 1,
    //! pointing along the arc) or from the inner edge to the outer (side -1, pointing back)
    private function roundEnd(pts as Array<[Numeric, Numeric]>, cx as Float, cy as Float, r as Float, w as Float, degrees as Number, dir as Number, side as Number) as Void {
        var a = Math.toRadians(degrees);
        var ex = cx + r * Math.cos(a);
        var ey = cy - r * Math.sin(a);
        var ux = Math.cos(a);   // outward, in screen coordinates
        var uy = -Math.sin(a);
        var tx = -dir * Math.sin(a);  // along the arc's travel
        var ty = -dir * Math.cos(a);
        for (var k = 1; k <= 5; k++) {
            var c = Math.cos(k * Math.PI / 6);
            var s = Math.sin(k * Math.PI / 6);
            pts.add([ex + side * w * (ux * c + tx * s), ey + side * w * (uy * c + ty * s)]);
        }
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

    //! "323 days to go", hidden when no race date is known or it has passed
    private function drawCountdown(dc as Dc, cx as Number, baseline as Number, days as Number?) as Void {
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
