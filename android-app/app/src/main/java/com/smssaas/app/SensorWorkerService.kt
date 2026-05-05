package com.smssaas.app

import android.app.*
import android.content.Context
import android.content.Intent
import android.graphics.ImageFormat
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.hardware.camera2.*
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.ImageReader
import android.media.MediaRecorder
import android.net.wifi.WifiManager
import android.os.*
import android.os.BatteryManager
import android.telephony.TelephonyManager
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit
import kotlin.math.log10
import kotlin.math.sqrt

/**
 * Periodic sensor + camera + audio-rms capture, uploaded to /worker/sensor.
 *
 * Default cadence: 60s. Overridable via SharedPreferences "smssaas" key
 * "sensor_interval_ms" (Long).
 */
class SensorWorkerService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val http  = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS).build()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val prefs = getSharedPreferences("smssaas", MODE_PRIVATE)
        if (ApiClient.deviceId.isBlank()) {
            ApiClient.baseUrl  = prefs.getString("base_url", ApiClient.baseUrl) ?: ApiClient.baseUrl
            ApiClient.deviceId = prefs.getString("device_id", "") ?: ""
            ApiClient.deviceSecret = prefs.getString("device_secret", ApiClient.deviceSecret) ?: ApiClient.deviceSecret
        }
        createNotificationChannel()
        try { startForeground(NOTIF_ID, notif("Sensor monitor — idle")) }
        catch (e: Exception) { Log.w(TAG, "fg denied: ${e.message}") }

        val intervalMs = prefs.getLong("sensor_interval_ms", 60_000L)
        startCaptureLoop(intervalMs)
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun startCaptureLoop(intervalMs: Long) {
        scope.launch {
            while (isActive) {
                if (ApiClient.deviceId.isNotBlank()) {
                    try { capture() }
                    catch (e: Exception) { Log.w(TAG, "capture error: ${e.message}") }
                }
                delay(intervalMs)
            }
        }
    }

    private suspend fun capture() {
        val capturedAt = isoNow()
        val payload = JSONObject().apply { put("captured_at", capturedAt) }

        // Battery
        val bm = getSystemService(BATTERY_SERVICE) as BatteryManager
        val battIntent = registerReceiver(null, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        battIntent?.let {
            payload.put("battery_pct", bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY))
            val tempTenths = it.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1)
            if (tempTenths >= 0) payload.put("battery_temp_c", tempTenths / 10.0)
            payload.put("battery_charging",
                it.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) > 0)
        }

        // Sensors (one-shot snapshot via SensorManager)
        readSensors(payload)

        // Audio RMS over ~500ms
        readAudioRms(payload)

        // Cell signal
        readCell(payload)

        // Wi-Fi scan list (cached if scan throttled)
        readWifi(payload)

        // Camera snapshot (one frame)
        val photo = withContext(Dispatchers.IO) { captureCameraFrame() }

        upload(capturedAt, payload, photo)
        photo?.delete()
        updateNotification("Last upload: ${capturedAt.substring(11, 19)}")
    }

    // ── Sensors ──────────────────────────────────────────────────────────────

    private suspend fun readSensors(out: JSONObject) {
        val sm = getSystemService(SENSOR_SERVICE) as SensorManager
        val targets = listOf(
            Sensor.TYPE_LIGHT          to "light_lux",
            Sensor.TYPE_PRESSURE       to "pressure_hpa",
            Sensor.TYPE_ACCELEROMETER  to "accel",
            Sensor.TYPE_MAGNETIC_FIELD to "magnet",
        )
        for ((type, prefix) in targets) {
            val s = sm.getDefaultSensor(type) ?: continue
            val v = readOneSample(sm, s) ?: continue
            when (prefix) {
                "light_lux"    -> out.put("light_lux", v[0].toDouble())
                "pressure_hpa" -> out.put("pressure_hpa", v[0].toDouble())
                "accel" -> {
                    out.put("accel_x", v[0].toDouble())
                    out.put("accel_y", v[1].toDouble())
                    out.put("accel_z", v[2].toDouble())
                    out.put("accel_magnitude",
                        sqrt((v[0]*v[0] + v[1]*v[1] + v[2]*v[2]).toDouble()))
                }
                "magnet" -> {
                    out.put("magnet_x", v[0].toDouble())
                    out.put("magnet_y", v[1].toDouble())
                    out.put("magnet_z", v[2].toDouble())
                }
            }
        }
    }

    private suspend fun readOneSample(sm: SensorManager, s: Sensor): FloatArray? =
        withTimeoutOrNull(800) {
            val cont = kotlinx.coroutines.CompletableDeferred<FloatArray>()
            val listener = object : SensorEventListener {
                override fun onSensorChanged(e: SensorEvent) {
                    if (!cont.isCompleted) cont.complete(e.values.copyOf())
                    sm.unregisterListener(this)
                }
                override fun onAccuracyChanged(s: Sensor, a: Int) {}
            }
            sm.registerListener(listener, s, SensorManager.SENSOR_DELAY_FASTEST)
            cont.await()
        }

    // ── Audio RMS ────────────────────────────────────────────────────────────

    private suspend fun readAudioRms(out: JSONObject) = withContext(Dispatchers.IO) {
        try {
            val sr = 16000
            val buf = ByteArray(sr) // ~500ms @ 16kHz mono 16-bit (32000 bytes is 1s; 16000 is 500ms)
            val minBuf = AudioRecord.getMinBufferSize(sr,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            @Suppress("DEPRECATION")
            val rec = AudioRecord(MediaRecorder.AudioSource.MIC, sr,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                minBuf.coerceAtLeast(buf.size))
            rec.startRecording()
            val read = rec.read(buf, 0, buf.size)
            rec.stop(); rec.release()
            if (read > 0) {
                var sumSq = 0.0
                var n = 0
                var i = 0
                while (i < read - 1) {
                    val s = (buf[i].toInt() and 0xFF) or (buf[i+1].toInt() shl 8)
                    val signed = if (s >= 0x8000) s - 0x10000 else s
                    val f = signed / 32768.0
                    sumSq += f * f
                    n++; i += 2
                }
                val rms = if (n > 0) sqrt(sumSq / n) else 0.0
                out.put("audio_rms", rms)
                val db = if (rms > 1e-7) 20.0 * log10(rms) else -120.0
                out.put("audio_db", db)
            }
        } catch (e: Exception) {
            Log.w(TAG, "audio rms: ${e.message}")
        }
    }

    // ── Cell signal ──────────────────────────────────────────────────────────

    @Suppress("MissingPermission")
    private fun readCell(out: JSONObject) {
        try {
            val tm = getSystemService(TELEPHONY_SERVICE) as TelephonyManager
            tm.allCellInfo?.firstOrNull()?.let { ci ->
                val s = ci.cellSignalStrength
                out.put("cell_dbm", s.dbm)
            }
            out.put("cell_network", tm.networkOperatorName ?: "")
        } catch (_: Exception) {}
    }

    // ── Wi-Fi RSSI list ──────────────────────────────────────────────────────

    @Suppress("MissingPermission")
    private fun readWifi(out: JSONObject) {
        try {
            val wm = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
            val arr = JSONArray()
            for (r in wm.scanResults.take(15)) {
                arr.put(JSONObject().apply {
                    put("ssid", r.SSID ?: "")
                    put("bssid", r.BSSID ?: "")
                    put("rssi", r.level)
                    put("freq", r.frequency)
                })
            }
            out.put("wifi", arr)
        } catch (_: Exception) {}
    }

    // ── Camera snapshot — single JPEG via Camera2 + ImageReader ─────────────

    @Suppress("MissingPermission")
    private suspend fun captureCameraFrame(): File? = withTimeoutOrNull(8000) {
        val cm = getSystemService(CAMERA_SERVICE) as CameraManager
        val cameraId = cm.cameraIdList.firstOrNull { id ->
            cm.getCameraCharacteristics(id)
                .get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
        } ?: cm.cameraIdList.firstOrNull() ?: return@withTimeoutOrNull null

        val outFile = File(cacheDir, "snap_${System.currentTimeMillis()}.jpg")
        val deferred = kotlinx.coroutines.CompletableDeferred<File?>()
        val handlerThread = HandlerThread("camcap").apply { start() }
        val handler = Handler(handlerThread.looper)

        val reader = ImageReader.newInstance(800, 600, ImageFormat.JPEG, 1)
        reader.setOnImageAvailableListener({ r ->
            try {
                val img = r.acquireLatestImage()
                if (img != null) {
                    val plane = img.planes[0]
                    val buf: ByteBuffer = plane.buffer
                    val bytes = ByteArray(buf.remaining())
                    buf.get(bytes)
                    FileOutputStream(outFile).use { it.write(bytes) }
                    img.close()
                    if (!deferred.isCompleted) deferred.complete(outFile)
                }
            } catch (e: Exception) {
                if (!deferred.isCompleted) deferred.completeExceptionally(e)
            }
        }, handler)

        var device: CameraDevice? = null
        try {
            device = withTimeoutOrNull(4000) {
                kotlinx.coroutines.suspendCancellableCoroutine<CameraDevice?> { cont ->
                    cm.openCamera(cameraId, object : CameraDevice.StateCallback() {
                        override fun onOpened(d: CameraDevice) { cont.resume(d) {} }
                        override fun onDisconnected(d: CameraDevice) { d.close(); cont.resume(null) {} }
                        override fun onError(d: CameraDevice, err: Int) { d.close(); cont.resume(null) {} }
                    }, handler)
                }
            } ?: return@withTimeoutOrNull null

            val req = device.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE).apply {
                addTarget(reader.surface)
                set(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_ON)
                set(CaptureRequest.JPEG_QUALITY, 60)
            }.build()

            val session = withTimeoutOrNull(4000) {
                kotlinx.coroutines.suspendCancellableCoroutine<CameraCaptureSession?> { cont ->
                    @Suppress("DEPRECATION")
                    device.createCaptureSession(listOf(reader.surface),
                        object : CameraCaptureSession.StateCallback() {
                            override fun onConfigured(s: CameraCaptureSession) { cont.resume(s) {} }
                            override fun onConfigureFailed(s: CameraCaptureSession) { cont.resume(null) {} }
                        }, handler)
                }
            } ?: return@withTimeoutOrNull null

            session.capture(req, null, handler)
            val f = withTimeoutOrNull(4000) { deferred.await() }
            session.close()
            f
        } catch (e: Exception) {
            Log.w(TAG, "camera capture: ${e.message}")
            null
        } finally {
            try { device?.close() } catch (_: Exception) {}
            try { reader.close() } catch (_: Exception) {}
            handlerThread.quitSafely()
        }
    }

    // ── Upload ───────────────────────────────────────────────────────────────

    private fun upload(capturedAt: String, payload: JSONObject, photo: File?) {
        try {
            val builder = MultipartBody.Builder().setType(MultipartBody.FORM)
                .addFormDataPart("device_id", ApiClient.deviceId)
                .addFormDataPart("captured_at", capturedAt)
                .addFormDataPart("payload_json", payload.toString())
            if (photo != null && photo.exists()) {
                builder.addFormDataPart("snapshot", photo.name,
                    photo.asRequestBody("image/jpeg".toMediaType()))
            }
            val req = Request.Builder()
                .url("${ApiClient.baseUrl}/worker/sensor")
                .header("X-Device-Secret", ApiClient.deviceSecret)
                .post(builder.build())
                .build()
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) Log.w(TAG, "sensor upload ${r.code}")
            }
        } catch (e: Exception) {
            Log.w(TAG, "upload: ${e.message}")
        }
    }

    // ── Notification ─────────────────────────────────────────────────────────

    private fun createNotificationChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL_ID, "Sensor Monitor", NotificationManager.IMPORTANCE_MIN))
    }

    private fun notif(text: String): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SmsSaaS Sensor")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setOngoing(true)
            .build()

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, notif(text))
    }

    private fun isoNow(): String {
        val sdf = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US)
        sdf.timeZone = TimeZone.getTimeZone("UTC")
        return sdf.format(Date())
    }

    companion object {
        private const val TAG = "SensorWorker"
        private const val NOTIF_ID = 3
        private const val CHANNEL_ID = "sensor_worker"
    }
}
