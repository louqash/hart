import Toybox.Application;
import Toybox.Background;
import Toybox.Lang;
import Toybox.System;
import Toybox.Time;
import Toybox.WatchUi;

(:background)
class HartApp extends Application.AppBase {
    //! How often the background service asks hart for new values
    private const FETCH_EVERY = 15 * 60;

    function initialize() {
        AppBase.initialize();
    }

    //! Foreground only: the view and its fonts never load in the background process
    (:typecheck(disableBackgroundCheck))
    function getInitialView() as [WatchUi.Views] or [WatchUi.Views, WatchUi.InputDelegates] {
        scheduleFetch();
        return [new $.HartView()];
    }

    function getServiceDelegate() as [System.ServiceDelegate] {
        return [new HartService()];
    }

    //! A reply from HartService: keep the last good values, with when they arrived, and the
    //! last failure's code (HTTP status or a Communications error) for the face to show
    function onBackgroundData(data as Application.PersistableType) as Void {
        if (data instanceof Dictionary && data.hasKey("error")) {
            Application.Storage.setValue("hartError", data["error"] as Number);
        } else if (data instanceof Dictionary) {
            Application.Storage.setValue("hart", data as Dictionary<Application.Storage.KeyType, Application.Storage.ValueType>);
            Application.Storage.setValue("hartAt", Time.now().value());
            Application.Storage.deleteValue("hartError");
        }
        repeatFetch();
        WatchUi.requestUpdate();
    }

    private function repeatFetch() as Void {
        if (!(Background.getTemporalEventRegisteredTime() instanceof Time.Duration)) {
            Background.registerForTemporalEvent(new Time.Duration(FETCH_EVERY));
        }
    }

    private function scheduleFetch() as Void {
        if (Config.hartUrl().length() == 0) {
            Background.deleteTemporalEvent();
            Application.Storage.deleteValue("hart");
            Application.Storage.deleteValue("hartError");
            return;
        }
        // The face restarts often (after activities, menus, reinstalls). Leave a pending schedule
        // alone: re-registering could keep pushing the next fetch out.
        if (Background.getTemporalEventRegisteredTime() != null) {
            return;
        }
        // Never tried yet: fetch once as soon as the 5-minute minimum between background runs
        // allows, instead of waiting a full interval. The reply starts the regular schedule.
        if (Application.Storage.getValue("hart") == null && Application.Storage.getValue("hartError") == null) {
            var last = Background.getLastTemporalEventTime();
            var soonest = last == null ? Time.now() : last.add(new Time.Duration(5 * 60));
            Background.registerForTemporalEvent(soonest.greaterThan(Time.now()) ? soonest : Time.now());
            return;
        }
        repeatFetch();
    }
}
