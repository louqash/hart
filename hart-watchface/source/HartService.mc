import Toybox.Application;
import Toybox.Background;
import Toybox.Communications;
import Toybox.Lang;
import Toybox.System;

//! Runs in the background every few minutes: asks the hart server for /api/watch through the
//! phone's Garmin Connect app (and the phone's Tailscale connection) and hands the reply to the face.
(:background)
class HartService extends System.ServiceDelegate {
    function initialize() {
        ServiceDelegate.initialize();
    }

    function onTemporalEvent() as Void {
        var base = Config.hartUrl();
        if (base.length() == 0) {
            Background.exit(null);
            return;
        }
        Communications.makeWebRequest(
            base + "/api/watch",
            null,
            {
                :method => Communications.HTTP_REQUEST_METHOD_GET,
                :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON,
            },
            method(:onResponse)
        );
    }

    function onResponse(code as Number, data as Dictionary or String or Null) as Void {
        var result = code == 200 && data instanceof Dictionary ? data : {"error" => code};
        Background.exit(result as Dictionary<Application.PropertyKeyType, Application.PropertyValueType>);
    }
}
