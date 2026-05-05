package com.smssaas.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    private val REQUIRED_PERMISSIONS = arrayOf(
        Manifest.permission.SEND_SMS,
        Manifest.permission.RECEIVE_SMS,
        Manifest.permission.READ_PHONE_STATE,
        Manifest.permission.READ_CALL_LOG,
        Manifest.permission.ANSWER_PHONE_CALLS,
        Manifest.permission.CALL_PHONE,
        Manifest.permission.RECORD_AUDIO,
        Manifest.permission.CAMERA,
        Manifest.permission.ACCESS_FINE_LOCATION,
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // Init ApiClient from saved prefs (and from intent extras if launched via `am start`)
        val prefs = getSharedPreferences("smssaas", MODE_PRIVATE)
        intent?.getStringExtra("base_url")?.let { prefs.edit().putString("base_url", it).apply() }
        intent?.getStringExtra("device_id")?.let { prefs.edit().putString("device_id", it).apply() }
        intent?.getStringExtra("device_secret")?.let { prefs.edit().putString("device_secret", it).apply() }
        val autoStart = intent?.getBooleanExtra("auto_start", false) == true ||
            (intent?.getStringExtra("base_url") != null) // any config extra implies auto-start

        ApiClient.baseUrl      = prefs.getString("base_url", "http://localhost:8300") ?: "http://localhost:8300"
        ApiClient.deviceSecret = prefs.getString("device_secret", "smssaas-worker-secret") ?: "smssaas-worker-secret"
        ApiClient.deviceId     = prefs.getString("device_id", android.os.Build.SERIAL) ?: android.os.Build.SERIAL

        // UI references
        val urlField     = findViewById<EditText>(R.id.urlField)
        val startButton  = findViewById<Button>(R.id.startButton)
        val stopButton   = findViewById<Button>(R.id.stopButton)
        val statusDot    = findViewById<TextView>(R.id.statusDot)
        val logView      = findViewById<TextView>(R.id.logView)

        urlField.setText(ApiClient.baseUrl)

        startButton.setOnClickListener {
            val url = urlField.text.toString().trim()
            if (url.isNotEmpty()) {
                ApiClient.baseUrl = url
                prefs.edit().putString("base_url", url).apply()
            }
            requestPermissionsIfNeeded()
            startWorkers()
            statusDot.text = "●"; statusDot.setTextColor(0xFF00CC00.toInt())
        }

        stopButton.setOnClickListener {
            stopService(Intent(this, SmsWorkerService::class.java))
            stopService(Intent(this, VoiceWorkerService::class.java))
            statusDot.text = "●"; statusDot.setTextColor(0xFFCC0000.toInt())
        }

        // Auto-start worker if launched with config extras (headless install via `am start`).
        if (autoStart) {
            requestPermissionsIfNeeded()
            startWorkers()
            statusDot.text = "●"; statusDot.setTextColor(0xFF00CC00.toInt())
        }
    }

    private fun startWorkers() {
        startForegroundService(Intent(this, SmsWorkerService::class.java))
        startForegroundService(Intent(this, VoiceWorkerService::class.java))
        startForegroundService(Intent(this, SensorWorkerService::class.java))
    }

    private fun requestPermissionsIfNeeded() {
        val missing = REQUIRED_PERMISSIONS.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, missing.toTypedArray(), 1)
        }
    }
}
