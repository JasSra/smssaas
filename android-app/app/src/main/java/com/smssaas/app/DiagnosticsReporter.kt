package com.smssaas.app

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.telephony.PhoneStateListener
import android.telephony.SignalStrength
import android.telephony.TelephonyManager
import android.util.Log
import kotlinx.coroutines.*

object DiagnosticsReporter {

    private const val TAG = "DiagnosticsReporter"
    private const val INTERVAL_MS = 60_000L

    @Volatile private var lastSignalDbm: Int? = null
    @Volatile private var lastCarrier: String? = null
    @Volatile private var lastNetworkType: String? = null

    fun start(context: Context, scope: CoroutineScope) {
        val tm = context.getSystemService(TelephonyManager::class.java)

        // Register a signal strength listener
        @Suppress("DEPRECATION")
        tm.listen(object : PhoneStateListener() {
            @Deprecated("Deprecated in Java")
            override fun onSignalStrengthsChanged(signalStrength: SignalStrength) {
                // getCellSignalStrengths() returns typed measurements; use dBm from the first one
                val dbm = signalStrength.cellSignalStrengths
                    .firstOrNull()
                    ?.dbm
                if (dbm != null && dbm != Int.MIN_VALUE) lastSignalDbm = dbm
            }
        }, PhoneStateListener.LISTEN_SIGNAL_STRENGTHS)

        scope.launch {
            while (isActive) {
                delay(INTERVAL_MS)
                try {
                    report(context, tm)
                } catch (e: Exception) {
                    Log.w(TAG, "report error: ${e.message}")
                }
            }
        }
    }

    private fun report(context: Context, tm: TelephonyManager) {
        val battery = context.registerReceiver(
            null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)
        )
        val batteryPct = battery?.let {
            val level = it.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
            val scale = it.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
            if (level >= 0 && scale > 0) (level * 100 / scale) else null
        }

        val carrier = tm.networkOperatorName.takeIf { it.isNotBlank() }
        val dataState = when (tm.dataState) {
            TelephonyManager.DATA_CONNECTED    -> "connected"
            TelephonyManager.DATA_DISCONNECTED -> "disconnected"
            TelephonyManager.DATA_CONNECTING   -> "connecting"
            else                               -> "unknown"
        }
        val networkType = networkTypeName(tm.dataNetworkType)

        lastCarrier = carrier
        lastNetworkType = networkType

        ApiClient.postDiagnostic(
            signalDbm  = lastSignalDbm,
            batteryPct = batteryPct,
            carrier    = carrier,
            networkType = networkType,
            dataState  = dataState,
        )
        Log.d(TAG, "diag: signal=$lastSignalDbm battery=$batteryPct% net=$networkType data=$dataState")
    }

    private fun networkTypeName(type: Int) = when (type) {
        TelephonyManager.NETWORK_TYPE_LTE    -> "LTE"
        TelephonyManager.NETWORK_TYPE_NR     -> "5G"
        TelephonyManager.NETWORK_TYPE_HSPA,
        TelephonyManager.NETWORK_TYPE_HSPAP  -> "HSPA"
        TelephonyManager.NETWORK_TYPE_UMTS   -> "3G"
        TelephonyManager.NETWORK_TYPE_EDGE,
        TelephonyManager.NETWORK_TYPE_GPRS   -> "2G"
        else                                 -> "unknown"
    }
}
