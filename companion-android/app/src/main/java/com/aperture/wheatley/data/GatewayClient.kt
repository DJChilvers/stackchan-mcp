package com.aperture.wheatley.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Result of an API call: either parsed data or a human-readable error. */
sealed interface Outcome<out T> {
    data class Ok<T>(val data: T) : Outcome<T>
    data class Err(val message: String, val code: Int = 0) : Outcome<Nothing>
}

private val JSON = Json { ignoreUnknownKeys = true; isLenient = true }
private val MEDIA = "application/json; charset=utf-8".toMediaType()

/**
 * Thin suspend wrapper over the gateway's companion (/api) endpoints. One
 * instance per [GatewayConfig]; rebuilt when settings change. All calls run on
 * Dispatchers.IO and never throw — failures come back as [Outcome.Err].
 */
class GatewayClient(private val config: GatewayConfig) {

    private val http = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private fun url(path: String) = config.baseUrl + path

    private fun Request.Builder.auth(): Request.Builder =
        if (config.token.isNotBlank()) header("Authorization", "Bearer ${config.token}") else this

    private suspend fun raw(request: Request): Outcome<String> = withContext(Dispatchers.IO) {
        try {
            http.newCall(request).execute().use { resp ->
                val body = resp.body?.string().orEmpty()
                if (resp.isSuccessful) {
                    Outcome.Ok(body)
                } else {
                    val msg = runCatching { JSONObject(body).optString("error") }
                        .getOrNull()?.takeIf { it.isNotBlank() } ?: "HTTP ${resp.code}"
                    Outcome.Err(msg, resp.code)
                }
            }
        } catch (e: Exception) {
            Outcome.Err(e.message ?: "network error")
        }
    }

    private suspend fun get(path: String): Outcome<String> =
        raw(Request.Builder().url(url(path)).auth().get().build())

    private suspend fun post(path: String, json: JSONObject = JSONObject()): Outcome<String> =
        raw(Request.Builder().url(url(path)).auth().post(json.toString().toRequestBody(MEDIA)).build())

    // ---- typed endpoints --------------------------------------------------
    suspend fun health(): Outcome<Boolean> = when (val r = get("/api/health")) {
        is Outcome.Ok -> Outcome.Ok(JSONObject(r.data).optBoolean("device_connected"))
        is Outcome.Err -> r
    }

    suspend fun status(): Outcome<StatusResponse> = when (val r = get("/api/status")) {
        is Outcome.Ok -> runCatching { JSON.decodeFromString<StatusResponse>(r.data) }
            .fold({ Outcome.Ok(it) }, { Outcome.Err("bad status payload: ${it.message}") })
        is Outcome.Err -> r
    }

    suspend fun sayCategories(): Outcome<List<SayCategory>> =
        when (val r = get("/api/say/categories")) {
            is Outcome.Ok -> runCatching {
                JSON.decodeFromString<SayCategoriesResponse>(r.data).categories
            }.fold({ Outcome.Ok(it) }, { Outcome.Err("bad categories payload") })
            is Outcome.Err -> r
        }

    suspend fun head(yaw: Int, pitch: Int, speed: Int = 0): Outcome<String> =
        post("/api/head", JSONObject().put("yaw", yaw).put("pitch", pitch).put("speed", speed))

    suspend fun torque(yaw: Boolean, pitch: Boolean): Outcome<String> =
        post("/api/torque", JSONObject().put("yaw", yaw).put("pitch", pitch))

    suspend fun avatar(face: String): Outcome<String> =
        post("/api/avatar", JSONObject().put("face", face))

    suspend fun leds(r: Int, g: Int, b: Int): Outcome<String> =
        post("/api/leds", JSONObject().put("r", r).put("g", g).put("b", b))

    suspend fun ledsClear(): Outcome<String> =
        post("/api/leds", JSONObject().put("clear", true))

    suspend fun volume(v: Int): Outcome<String> =
        post("/api/volume", JSONObject().put("volume", v))

    suspend fun brightness(v: Int): Outcome<String> =
        post("/api/brightness", JSONObject().put("brightness", v))

    suspend fun motor(direction: String, speed: Int): Outcome<String> =
        post("/api/motor", JSONObject().put("direction", direction).put("speed", speed))

    suspend fun say(text: String): Outcome<String> =
        post("/api/say", JSONObject().put("text", text))

    suspend fun sayPreset(category: String): Outcome<String> =
        post("/api/say/preset", JSONObject().put("category", category))

    suspend fun react(behavior: String): Outcome<String> =
        post("/api/react/$behavior")

    suspend fun demo(): Outcome<String> = post("/api/demo")
}
