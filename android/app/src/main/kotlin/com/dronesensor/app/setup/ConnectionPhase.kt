package com.dronesensor.app.setup

/**
 * Coarse-grained connection phase surfaced to the installer UI.
 *
 * The phases are ordered by "how close are we to a working session" so
 * the UI can render a single status string + color without branching on
 * exact combinations of fields.
 */
enum class ConnectionPhase(val installerLabel: String) {
    NO_NETWORK("No internet"),
    NO_SIM_NO_WIFI("No SIM card and no Wi-Fi configured"),
    SEARCHING_CELLULAR("Searching for cellular"),
    CELLULAR_AVAILABLE("Cellular available"),
    WIFI_AVAILABLE("Wi-Fi available"),
    WIFI_CONNECTED("Wi-Fi connected"),
    CLOUD_REACHABLE("Reaching cloud"),
    CLOUD_AUTHENTICATED("Cloud check-in succeeded"),
    ;

    val isTerminalSuccess: Boolean
        get() = this == CLOUD_AUTHENTICATED
}
