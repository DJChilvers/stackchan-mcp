package com.aperture.wheatley.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import coil.ImageLoader
import coil.compose.AsyncImage
import coil.compose.AsyncImagePainter
import coil.request.CachePolicy
import coil.request.ImageRequest
import com.aperture.wheatley.MainViewModel
import com.aperture.wheatley.data.CameraMeta
import com.aperture.wheatley.data.Outcome
import com.aperture.wheatley.ui.components.AperturePanel
import com.aperture.wheatley.ui.components.ApertureButton
import com.aperture.wheatley.ui.theme.ApertureAmber
import com.aperture.wheatley.ui.theme.ApertureBlack
import com.aperture.wheatley.ui.theme.AperturePortal
import com.aperture.wheatley.ui.theme.ApertureTextDim
import kotlinx.coroutines.delay
import okhttp3.OkHttpClient

private const val SNAPSHOT_INTERVAL_MS = 2000L

@Composable
fun CameraScreen(vm: MainViewModel) {
    val context = LocalContext.current
    val cfg by vm.config.collectAsState()
    val visionBusy by vm.visionBusy.collectAsState()
    val visionAnswer by vm.visionAnswer.collectAsState()

    // Cache-busting tick that drives both the snapshot refetch and the meta poll.
    var tick by remember { mutableLongStateOf(0L) }
    var meta by remember { mutableStateOf<CameraMeta?>(null) }
    var lastError by remember { mutableStateOf<String?>(null) }
    var question by remember { mutableStateOf("") }

    // A Coil loader that never caches (so each tick refetches) and injects the
    // Bearer token when one is configured.
    val imageLoader = remember(cfg.token) {
        val ok = OkHttpClient.Builder().apply {
            if (cfg.token.isNotBlank()) addInterceptor { chain ->
                chain.proceed(
                    chain.request().newBuilder()
                        .header("Authorization", "Bearer ${cfg.token}")
                        .build()
                )
            }
        }.build()
        ImageLoader.Builder(context)
            .okHttpClient(ok)
            .memoryCachePolicy(CachePolicy.DISABLED)
            .diskCachePolicy(CachePolicy.DISABLED)
            .build()
    }

    // Drive the refresh loop + recognition-meta poll.
    LaunchedEffect(cfg) {
        val client = vm.client()
        while (true) {
            tick = System.currentTimeMillis()
            when (val r = client.cameraMeta()) {
                is Outcome.Ok -> meta = r.data
                is Outcome.Err -> {}
            }
            delay(SNAPSHOT_INTERVAL_MS)
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        AperturePanel("Optic feed") {
            Box(
                Modifier
                    .fillMaxWidth()
                    .aspectRatio(4f / 3f)
                    .clip(RoundedCornerShape(3.dp)),
                contentAlignment = Alignment.Center,
            ) {
                AsyncImage(
                    model = ImageRequest.Builder(context)
                        .data(vm.client().snapshotUrl(tick))
                        .crossfade(false)
                        .build(),
                    imageLoader = imageLoader,
                    contentDescription = "Wheatley camera",
                    modifier = Modifier.fillMaxSize(),
                    onState = { state ->
                        if (state is AsyncImagePainter.State.Error) {
                            lastError = state.result.throwable.message ?: "snapshot failed"
                        } else if (state is AsyncImagePainter.State.Success) {
                            lastError = null
                        }
                    },
                )
                if (tick == 0L) {
                    CircularProgressIndicator(color = AperturePortal)
                }
            }
            Spacer(Modifier.height(8.dp))
            RecognitionOverlay(meta, lastError)
        }

        AperturePanel("Look at this") {
            Text(
                "Ask Wheatley what he can see. He'll take a photo, think about it, and say the answer aloud.",
                style = MaterialTheme.typography.bodySmall,
                color = ApertureTextDim,
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = question,
                onValueChange = { question = it },
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("e.g. what am I holding?") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { if (!visionBusy) vm.askVision(question) }),
            )
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                ApertureButton(
                    if (visionBusy) "Looking…" else "Look at this",
                    modifier = Modifier.weight(1f),
                    enabled = !visionBusy,
                ) { vm.askVision(question) }
                if (visionBusy) {
                    Spacer(Modifier.height(0.dp))
                    CircularProgressIndicator(
                        color = ApertureAmber,
                        modifier = Modifier.padding(start = 12.dp).height(22.dp),
                        strokeWidth = 2.dp,
                    )
                }
            }
            visionAnswer?.let { answer ->
                Spacer(Modifier.height(10.dp))
                Box(
                    Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(3.dp)),
                ) {
                    Text(
                        "“$answer”",
                        style = MaterialTheme.typography.bodyMedium,
                        color = ApertureAmber,
                    )
                }
            }
        }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun RecognitionOverlay(meta: CameraMeta?, error: String?) {
    val (label, color) = when {
        error != null -> "SIGNAL LOST — ${error.take(48)}" to ApertureTextDim
        meta == null -> "SCANNING…" to ApertureTextDim
        meta.stale -> "VISION LOOP OFFLINE" to ApertureTextDim
        !meta.faceVisible -> "NO FACE DETECTED" to ApertureTextDim
        !meta.name.isNullOrBlank() -> "RECOGNISED: ${meta.name}" to ApertureAmber
        else -> "UNKNOWN FACE" to AperturePortal
    }
    Text(label, style = MaterialTheme.typography.labelLarge, color = color)
}
