import Toybox.Application;
import Toybox.Lang;

(:background)
module Config {
    //! The hart server's address, compiled in from resources-local/strings.xml; "" when unset
    function hartUrl() as String {
        return Application.loadResource(Rez.Strings.HartUrl) as String;
    }
}
