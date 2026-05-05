package com.smssaas.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            "com.smssaas.app.START_WORKER" -> {
                // Hydrate ApiClient from prefs (or from intent extras for one-shot setup)
                val prefs = context.getSharedPreferences("smssaas", Context.MODE_PRIVATE)
                intent.getStringExtra("base_url")?.let {
                    prefs.edit().putString("base_url", it).apply()
                }
                intent.getStringExtra("device_id")?.let {
                    prefs.edit().putString("device_id", it).apply()
                }
                intent.getStringExtra("device_secret")?.let {
                    prefs.edit().putString("device_secret", it).apply()
                }
                ApiClient.baseUrl      = prefs.getString("base_url", "http://localhost:8300") ?: "http://localhost:8300"
                ApiClient.deviceSecret = prefs.getString("device_secret", "smssaas-worker-secret") ?: "smssaas-worker-secret"
                ApiClient.deviceId     = prefs.getString("device_id", android.os.Build.SERIAL) ?: android.os.Build.SERIAL

                // On Android 14, startForegroundService from background broadcast may be denied.
                // Fall back to startService (the service will catch the startForeground error too).
                val intent = Intent(context, SmsWorkerService::class.java)
                try {
                    context.startForegroundService(intent)
                } catch (e: Exception) {
                    context.startService(intent)
                }
            }
        }
    }
}
