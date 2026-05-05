package com.smssaas.app

import android.util.Log
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

object ApiClient {
    private const val TAG = "ApiClient"

    // ADB reverse tunnel: phone's localhost:8300 → gateway VM's :8300.
    // watcher.sh runs `adb reverse tcp:8300 tcp:8300` per connected device.
    // Override at runtime via SharedPreferences "smssaas" / "base_url" if needed.
    var baseUrl = "http://localhost:8300"
    var deviceSecret = "smssaas-worker-secret"
    var deviceId = ""

    private val client = OkHttpClient.Builder()
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .writeTimeout(5, TimeUnit.SECONDS)
        .build()

    private val JSON = "application/json; charset=utf-8".toMediaType()

    fun pollJobs(): List<SmsJob> {
        val request = Request.Builder()
            .url("$baseUrl/worker/poll?device_id=$deviceId&limit=10")
            .header("X-Device-Secret", deviceSecret)
            .get()
            .build()

        return try {
            client.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) return emptyList()
                val body = JSONObject(resp.body!!.string())
                val jobs = body.getJSONArray("jobs")
                List(jobs.length()) { i ->
                    val j = jobs.getJSONObject(i)
                    SmsJob(
                        id = j.getInt("id"),
                        toNumber = j.getString("to_number"),
                        body = j.getString("body"),
                        mediaUrl = j.optString("media_url", "")
                    )
                }
            }
        } catch (e: IOException) {
            Log.w(TAG, "poll failed: ${e.message}")
            emptyList()
        }
    }

    fun postReceipt(messageId: Int, result: String, errorCode: Int? = null) {
        val json = JSONObject().apply {
            put("device_id", deviceId)
            put("message_id", messageId)
            put("result", result)
            if (errorCode != null) put("error_code", errorCode)
        }
        post("/worker/receipt", json)
    }

    fun postInbound(fromNumber: String, body: String, receivedAt: String) {
        val json = JSONObject().apply {
            put("device_id", deviceId)
            put("from_number", fromNumber)
            put("body", body)
            put("received_at", receivedAt)
        }
        post("/worker/inbound", json)
    }

    fun postDiagnostic(signalDbm: Int?, batteryPct: Int?, carrier: String?, networkType: String?, dataState: String?) {
        val json = JSONObject().apply {
            put("device_id", deviceId)
            if (signalDbm != null) put("signal_dbm", signalDbm)
            if (batteryPct != null) put("battery_pct", batteryPct)
            if (carrier != null) put("carrier", carrier)
            if (networkType != null) put("network_type", networkType)
            if (dataState != null) put("data_state", dataState)
        }
        post("/worker/diagnostic", json)
    }

    fun postHeartbeat() {
        post("/worker/heartbeat", JSONObject().put("device_id", deviceId))
    }

    /** Poll for pending device commands (dial, hangup, ...). */
    fun pollCommands(): List<DeviceCommand> {
        val request = Request.Builder()
            .url("$baseUrl/worker/poll/commands?device_id=$deviceId&limit=5")
            .header("X-Device-Secret", deviceSecret)
            .get()
            .build()
        return try {
            client.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) return emptyList()
                val body = JSONObject(resp.body!!.string())
                val arr = body.getJSONArray("commands")
                List(arr.length()) { i ->
                    val c = arr.getJSONObject(i)
                    DeviceCommand(
                        id = c.getInt("id"),
                        command = c.getString("command"),
                        payload = JSONObject(c.optString("payload_json", "{}")),
                    )
                }
            }
        } catch (e: IOException) {
            Log.w(TAG, "poll commands failed: ${e.message}")
            emptyList()
        }
    }

    /** Report a command outcome back to the server. result = "done" | "failed". */
    fun ackCommand(cmdId: Int, result: String = "done") {
        val request = Request.Builder()
            .url("$baseUrl/worker/command/ack?cmd_id=$cmdId&result=$result")
            .header("X-Device-Secret", deviceSecret)
            .post("".toRequestBody(JSON))
            .build()
        try {
            client.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) Log.w(TAG, "ack $cmdId → ${resp.code}")
            }
        } catch (e: IOException) {
            Log.w(TAG, "ack failed: ${e.message}")
        }
    }

    fun postCallState(callId: String, status: String, fromNumber: String? = null,
                      toNumber: String? = null, direction: String? = null, durationSec: Int? = null) {
        val json = JSONObject().apply {
            put("device_id", deviceId)
            put("call_id", callId)
            put("status", status)
            if (fromNumber != null) put("from_number", fromNumber)
            if (toNumber != null) put("to_number", toNumber)
            if (direction != null) put("direction", direction)
            if (durationSec != null) put("duration_sec", durationSec)
        }
        post("/worker/call/state", json)
    }

    private fun post(path: String, json: JSONObject) {
        val body = json.toString().toRequestBody(JSON)
        val request = Request.Builder()
            .url("$baseUrl$path")
            .header("X-Device-Secret", deviceSecret)
            .post(body)
            .build()
        try {
            client.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) Log.w(TAG, "POST $path → ${resp.code}")
            }
        } catch (e: IOException) {
            Log.w(TAG, "POST $path failed: ${e.message}")
        }
    }

    // WebSocket URL for voice audio streaming
    fun voiceWsUrl(callId: String) = baseUrl
        .replace("http://", "ws://")
        .replace("https://", "wss://") +
        "/ws/voice/${deviceId}/${callId}"
}

data class SmsJob(
    val id: Int,
    val toNumber: String,
    val body: String,
    val mediaUrl: String,
)

data class DeviceCommand(
    val id: Int,
    val command: String,        // 'dial' | 'hangup' | ...
    val payload: JSONObject,    // command-specific args
)
