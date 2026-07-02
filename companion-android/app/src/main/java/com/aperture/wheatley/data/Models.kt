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
