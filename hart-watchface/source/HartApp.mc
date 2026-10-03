import Toybox.Application;
import Toybox.Lang;
import Toybox.WatchUi;

class HartApp extends Application.AppBase {
    function initialize() {
        AppBase.initialize();
    }

    function getInitialView() as [WatchUi.Views] or [WatchUi.Views, WatchUi.InputDelegates] {
        return [new HartView()];
    }

    //! Race name or date edited in the Connect IQ phone app
    function onSettingsChanged() as Void {
        WatchUi.requestUpdate();
    }
}
