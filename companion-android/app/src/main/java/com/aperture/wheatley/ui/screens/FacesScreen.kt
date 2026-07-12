package com.aperture.wheatley.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.aperture.wheatley.MainViewModel
import com.aperture.wheatley.data.FaceEntry
import com.aperture.wheatley.data.GatewayClient
import com.aperture.wheatley.data.Outcome
import com.aperture.wheatley.data.VisitorEntry
import com.aperture.wheatley.ui.components.AperturePanel
import com.aperture.wheatley.ui.components.ApertureButton
import com.aperture.wheatley.ui.components.ApertureOutlineButton
import com.aperture.wheatley.ui.components.rememberGatewayImageLoader
import com.aperture.wheatley.ui.theme.ApertureAmber
import com.aperture.wheatley.ui.theme.ApertureOutline
import com.aperture.wheatley.ui.theme.AperturePortal
import com.aperture.wheatley.ui.theme.ApertureTextDim
import kotlinx.coroutines.launch

@Composable
fun FacesScreen(vm: MainViewModel) {
    val cfg by vm.config.collectAsState()
    val scope = rememberCoroutineScope()
    val imageLoader = rememberGatewayImageLoader(cfg.token)

    var faces by remember { mutableStateOf<List<FaceEntry>>(emptyList()) }
    var visitors by remember { mutableStateOf<List<VisitorEntry>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var enrolling by remember { mutableStateOf(false) }

    var renameTarget by remember { mutableStateOf<FaceEntry?>(null) }
    var greetingTarget by remember { mutableStateOf<FaceEntry?>(null) }
    var deleteTarget by remember { mutableStateOf<FaceEntry?>(null) }
    var showEnroll by remember { mutableStateOf(false) }

    suspend fun reload() {
        val c = vm.client()
        (c.faces() as? Outcome.Ok)?.let { faces = it.data }
        (c.visitors() as? Outcome.Ok)?.let { visitors = it.data }
        loading = false
    }
    LaunchedEffect(cfg) { loading = true; reload() }

    fun act(label: String, block: suspend (GatewayClient) -> Outcome<*>) {
        scope.launch {
            when (val r = block(vm.client())) {
                is Outcome.Ok -> { vm.toast("$label ✓"); reload() }
                is Outcome.Err -> vm.toast("$label ✗ ${r.message}")
            }
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        AperturePanel("Known faces") {
            if (loading) {
                Row(Modifier.fillMaxWidth().padding(8.dp), horizontalArrangement = Arrangement.Center) {
                    CircularProgressIndicator(color = AperturePortal)
                }
            } else if (faces.isEmpty()) {
                Text(
                    "No enrolled faces yet. Point the camera at someone and enrol them below.",
                    style = MaterialTheme.typography.bodySmall, color = ApertureTextDim,
                )
            } else {
                faces.forEachIndexed { i, face ->
                    if (i > 0) Spacer(Modifier.height(10.dp))
                    FaceRow(
                        face = face,
                        photoUrl = if (face.hasPhoto) vm.client().facePhotoUrl(face.name) else null,
                        imageLoader = imageLoader,
                        onRename = { renameTarget = face },
                        onGreeting = { greetingTarget = face },
                        onDelete = { deleteTarget = face },
                    )
                }
            }
            Spacer(Modifier.height(12.dp))
            ApertureButton(
                if (enrolling) "Enrolling…" else "Enrol current view",
                modifier = Modifier.fillMaxWidth(),
                enabled = !enrolling,
            ) { showEnroll = true }
            Text(
                "Enrol captures a few photos through Wheatley's camera — make sure the person is in frame.",
                style = MaterialTheme.typography.bodySmall, color = ApertureTextDim,
                modifier = Modifier.padding(top = 6.dp),
            )
        }

        AperturePanel("Visitor log") {
            if (visitors.isEmpty()) {
                Text(
                    "No recognition events recorded yet.",
                    style = MaterialTheme.typography.bodySmall, color = ApertureTextDim,
                )
            } else {
                visitors.forEachIndexed { i, v ->
                    if (i > 0) Spacer(Modifier.height(8.dp))
                    VisitorRow(v, vm.client().visitorThumbUrl(v.id), imageLoader)
                }
            }
        }
        Spacer(Modifier.height(24.dp))
    }

    // ---- dialogs ---------------------------------------------------------
    renameTarget?.let { target ->
        TextPromptDialog(
            title = "Rename ${target.name}",
            label = "New name",
            initial = target.name,
            confirm = "Rename",
            onDismiss = { renameTarget = null },
            onConfirm = { newName ->
                renameTarget = null
                act("Rename") { it.renameFace(target.name, newName) }
            },
        )
    }
    greetingTarget?.let { target ->
        TextPromptDialog(
            title = "Greeting for ${target.name}",
            label = "Custom line ({name} allowed, blank to clear)",
            initial = target.greeting ?: "",
            confirm = "Save",
            onDismiss = { greetingTarget = null },
            onConfirm = { line ->
                greetingTarget = null
                act("Greeting") { it.setGreeting(target.name, line) }
            },
        )
    }
    deleteTarget?.let { target ->
        AlertDialog(
            onDismissRequest = { deleteTarget = null },
            title = { Text("Forget ${target.name}?") },
            text = {
                Text(
                    "This deletes their ${target.samples} sample(s), reference photo and greeting. " +
                        "Wheatley won't recognise them until re-enrolled.",
                    color = ApertureTextDim,
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    val t = target
                    deleteTarget = null
                    act("Forget") { it.deleteFace(t.name) }
                }) { Text("Forget", color = ApertureAmber) }
            },
            dismissButton = { TextButton(onClick = { deleteTarget = null }) { Text("Cancel") } },
        )
    }
    if (showEnroll) {
        TextPromptDialog(
            title = "Enrol a face",
            label = "Name",
            initial = "",
            confirm = "Enrol",
            onDismiss = { showEnroll = false },
            onConfirm = { name ->
                showEnroll = false
                if (name.isNotBlank()) {
                    scope.launch {
                        enrolling = true
                        when (val r = vm.client().enrollFace(name)) {
                            is Outcome.Ok -> { vm.toast("Enrolled $name ✓"); reload() }
                            is Outcome.Err -> vm.toast("Enrol ✗ ${r.message}")
                        }
                        enrolling = false
                    }
                }
            },
        )
    }
}

@Composable
private fun FaceRow(
    face: FaceEntry,
    photoUrl: String?,
    imageLoader: coil.ImageLoader,
    onRename: () -> Unit,
    onGreeting: () -> Unit,
    onDelete: () -> Unit,
) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Avatar(photoUrl, face.name, imageLoader, 48.dp)
        Spacer(Modifier.size(12.dp))
        Column(Modifier.weight(1f)) {
            Text(face.name, style = MaterialTheme.typography.bodyMedium, color = ApertureAmber)
            Text(
                "${face.samples} sample${if (face.samples == 1) "" else "s"}",
                style = MaterialTheme.typography.bodySmall, color = ApertureTextDim,
            )
            face.greeting?.let {
                Text(
                    "“$it”",
                    style = MaterialTheme.typography.bodySmall,
                    color = AperturePortal,
                    fontStyle = FontStyle.Italic,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.padding(top = 6.dp)) {
        ApertureOutlineButton("Rename", modifier = Modifier.weight(1f), onClick = onRename)
        ApertureOutlineButton("Greeting", modifier = Modifier.weight(1f), onClick = onGreeting)
        ApertureOutlineButton("Forget", modifier = Modifier.weight(1f), onClick = onDelete)
    }
}

@Composable
private fun VisitorRow(v: VisitorEntry, thumbUrl: String, imageLoader: coil.ImageLoader) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Avatar(if (v.thumb != null) thumbUrl else null, v.name ?: "?", imageLoader, 40.dp)
        Spacer(Modifier.size(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                if (v.known && v.name != null) v.name else "Unknown face",
                style = MaterialTheme.typography.bodyMedium,
                color = if (v.known) ApertureAmber else AperturePortal,
            )
            Text(
                "${timeAgo(v.ts)} · score ${"%.2f".format(v.score)}",
                style = MaterialTheme.typography.bodySmall, color = ApertureTextDim,
            )
        }
    }
}

@Composable
private fun Avatar(url: String?, name: String, imageLoader: coil.ImageLoader, size: androidx.compose.ui.unit.Dp) {
    Box(
        Modifier.size(size).clip(CircleShape).background(ApertureOutline),
        contentAlignment = Alignment.Center,
    ) {
        if (url != null) {
            AsyncImage(
                model = url,
                imageLoader = imageLoader,
                contentDescription = name,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        } else {
            Text(
                name.firstOrNull()?.uppercase() ?: "?",
                style = MaterialTheme.typography.titleMedium, color = ApertureAmber,
            )
        }
    }
}

@Composable
private fun TextPromptDialog(
    title: String,
    label: String,
    initial: String,
    confirm: String,
    onDismiss: () -> Unit,
    onConfirm: (String) -> Unit,
) {
    var text by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            OutlinedTextField(
                value = text,
                onValueChange = { text = it },
                label = { Text(label, style = MaterialTheme.typography.bodySmall) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
        },
        confirmButton = { TextButton(onClick = { onConfirm(text.trim()) }) { Text(confirm, color = ApertureAmber) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

/** Compact relative time from a unix-seconds timestamp. */
private fun timeAgo(tsSeconds: Double): String {
    val secs = (System.currentTimeMillis() / 1000.0 - tsSeconds).toLong()
    return when {
        secs < 60 -> "just now"
        secs < 3600 -> "${secs / 60}m ago"
        secs < 86400 -> "${secs / 3600}h ago"
        else -> "${secs / 86400}d ago"
    }
}
