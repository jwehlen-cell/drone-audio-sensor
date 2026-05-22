package com.dronesensor.app

import android.app.Application
import com.dronesensor.app.config.AppConfig
import com.dronesensor.app.identity.DeviceIdentity

class DroneSensorApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        AppConfig.get(this)
        DeviceIdentity.get(this)
    }
}
