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
class GatewayClient(val config: GatewayConfig) {

    private val http = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        // Vision ("look at this") captures a photo then calls Claude — allow for it.
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    private fun url(path: String) = config.baseUrl + path

    /** Absolute URL for a fresh camera snapshot, cache-busted by [tick]. */
    fun snapshotUrl(tick: Long): String = url("/api/camera/snapshot?t=$tick")

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

    private suspend fun put(path: String, json: JSONObject = JSONObject()): Outcome<String> =
        raw(Request.Builder().url(url(path)).auth().put(json.toString().toRequestBody(MEDIA)).build())

    private suspend fun delete(path: String): Outcome<String> =
        raw(Request.Builder().url(url(path)).auth().delete().build())

    private fun enc(s: String): String =
        java.net.URLEncoder.encode(s, "UTF-8").replace("+", "%20")

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

    // ---- camera + vision (Phase 3) ---------------------------------------
    suspend fun cameraMeta(): Outcome<CameraMeta> = when (val r = get("/api/camera/meta")) {
        is Outcome.Ok -> runCatching { JSON.decodeFromString<CameraMeta>(r.data) }
            .fold({ Outcome.Ok(it) }, { Outcome.Err("bad camera meta payload") })
        is Outcome.Err -> r
    }

    /** "Look at this": capture + Claude vision + speak; returns the answer. */
    suspend fun visionAsk(question: String): Outcome<VisionAnswer> {
        val payload = JSONObject().put("question", question)
        return when (val r = post("/api/vision/ask", payload)) {
            is Outcome.Ok -> runCatching { JSON.decodeFromString<VisionAnswer>(r.data) }
                .fold({ Outcome.Ok(it) }, { Outcome.Err("bad vision payload") })
            is Outcome.Err -> r
        }
    }

    // ---- faces roster + visitor log (Phase 4) ----------------------------
    suspend fun faces(): Outcome<List<FaceEntry>> = when (val r = get("/api/faces")) {
        is Outcome.Ok -> runCatching { JSON.decodeFromString<FacesResponse>(r.data).faces }
            .fold({ Outcome.Ok(it) }, { Outcome.Err("bad faces payload") })
        is Outcome.Err -> r
    }

    suspend fun renameFace(name: String, newName: String): Outcome<String> =
        post("/api/faces/${enc(name)}/rename", JSONObject().put("new_name", newName))

    suspend fun deleteFace(name: String): Outcome<String> =
        delete("/api/faces/${enc(name)}")

    suspend fun setGreeting(name: String, line: String): Outcome<String> =
        put("/api/faces/${enc(name)}/greeting", JSONObject().put("line", line))

    suspend fun enrollFace(name: String): Outcome<String> =
        post("/api/faces/enroll", JSONObject().put("name", name))

    /** URL for a person's reference photo (for Coil). */
    fun facePhotoUrl(name: String): String = url("/api/faces/${enc(name)}/photo")

    suspend fun visitors(): Outcome<List<VisitorEntry>> = when (val r = get("/api/visitors")) {
        is Outcome.Ok -> runCatching { JSON.decodeFromString<VisitorsResponse>(r.data).visitors }
            .fold({ Outcome.Ok(it) }, { Outcome.Err("bad visitors payload") })
        is Outcome.Err -> r
    }

    /** URL for a visitor-log thumbnail (for Coil). */
    fun visitorThumbUrl(id: String): String = url("/api/visitors/${enc(id)}/thumb")

    // ---- orientation (upside-down mount) ---------------------------------
    suspend fun getOrientation(): Outcome<Boolean> = when (val r = get("/api/orientation")) {
        is Outcome.Ok -> Outcome.Ok(JSONObject(r.data).optBoolean("upside_down"))
        is Outcome.Err -> r
    }

    suspend fun setOrientation(upsideDown: Boolean): Outcome<Boolean> =
        when (val r = post("/api/orientation", JSONObject().put("upside_down", upsideDown))) {
            is Outcome.Ok -> Outcome.Ok(JSONObject(r.data).optBoolean("upside_down"))
            is Outcome.Err -> r
        }
}
