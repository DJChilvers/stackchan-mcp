package com.aperture.wheatley.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class Battery(
    val level: Int? = null,
    val charging: Boolean? = null,
)

/** Response of GET /api/status. Unknown fields are ignored (see Json config). */
@Serializable
data class StatusResponse(
    val ok: Boolean = false,
    val connected: Boolean = false,
    @SerialName("device_id") val deviceId: String? = null,
    @SerialName("uptime_s") val uptimeS: Long = 0,
    val battery: Battery? = null,
    val volume: Int? = null,
    val brightness: Int? = null,
    @SerialName("device_status_error") val deviceStatusError: String? = null,
)

@Serializable
data class SayCategory(
    val key: String,
    val label: String,
)

@Serializable
data class SayCategoriesResponse(
    val ok: Boolean = false,
    val categories: List<SayCategory> = emptyList(),
)

/** Recognition overlay data from GET /api/camera/meta. */
@Serializable
data class CameraMeta(
    val ok: Boolean = false,
    @SerialName("face_visible") val faceVisible: Boolean = false,
    val person: String? = null,
    val name: String? = null,
    val stale: Boolean = false,
)

/** Response of POST /api/vision/ask ("Look at this"). */
@Serializable
data class VisionAnswer(
    val ok: Boolean = false,
    val question: String = "",
    val answer: String = "",
    val spoke: Boolean = false,
)

/** One enrolled person in GET /api/faces. */
@Serializable
data class FaceEntry(
    val name: String,
    val samples: Int = 0,
    @SerialName("has_photo") val hasPhoto: Boolean = false,
    val greeting: String? = null,
)

@Serializable
data class FacesResponse(
    val ok: Boolean = false,
    val faces: List<FaceEntry> = emptyList(),
)

/** One recognition event in GET /api/visitors. */
@Serializable
data class VisitorEntry(
    val id: String,
    val ts: Double = 0.0,
    val name: String? = null,
    val known: Boolean = false,
    val score: Double = 0.0,
    val thumb: String? = null,
)

@Serializable
data class VisitorsResponse(
    val ok: Boolean = false,
    val visitors: List<VisitorEntry> = emptyList(),
)
