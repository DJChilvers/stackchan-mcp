package com.aperture.wheatley.ui.components

import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import coil.ImageLoader
import coil.request.CachePolicy
import okhttp3.OkHttpClient

/**
 * A Coil [ImageLoader] that injects the gateway Bearer token (when set). Pass
 * [disableCache] = true for the live camera feed so each tick refetches; leave
 * it false for face photos / visitor thumbnails, whose URLs are already unique.
 */
@Composable
fun rememberGatewayImageLoader(token: String, disableCache: Boolean = false): ImageLoader {
    val context = LocalContext.current
    return remember(token, disableCache) {
        val ok = OkHttpClient.Builder().apply {
            if (token.isNotBlank()) addInterceptor { chain ->
                chain.proceed(
                    chain.request().newBuilder()
                        .header("Authorization", "Bearer $token")
                        .build()
                )
            }
        }.build()
        ImageLoader.Builder(context).okHttpClient(ok).apply {
            if (disableCache) {
                memoryCachePolicy(CachePolicy.DISABLED)
                diskCachePolicy(CachePolicy.DISABLED)
            }
        }.build()
    }
}
