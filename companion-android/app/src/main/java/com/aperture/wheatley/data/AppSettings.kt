package com.aperture.wheatley.data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "wheatley_settings")

/** Connection settings for reaching the gateway over the LAN. */
data class GatewayConfig(
    val host: String = "192.168.1.138",
    val port: Int = 8770,
    val token: String = "",
) {
    val baseUrl: String get() = "http://$host:$port"
    val isConfigured: Boolean get() = host.isNotBlank() && port in 1..65535
}

class AppSettings(private val context: Context) {
    private object Keys {
        val HOST = stringPreferencesKey("host")
        val PORT = stringPreferencesKey("port")
        val TOKEN = stringPreferencesKey("token")
    }

    val config: Flow<GatewayConfig> = context.dataStore.data.map { p ->
        GatewayConfig(
            host = p[Keys.HOST] ?: GatewayConfig().host,
            port = p[Keys.PORT]?.toIntOrNull() ?: GatewayConfig().port,
            token = p[Keys.TOKEN] ?: "",
        )
    }

    suspend fun save(config: GatewayConfig) {
        context.dataStore.edit { p ->
            p[Keys.HOST] = config.host.trim()
            p[Keys.PORT] = config.port.toString()
            p[Keys.TOKEN] = config.token.trim()
        }
    }
}
