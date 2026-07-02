package com.aperture.wheatley.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.aperture.wheatley.ui.theme.ApertureAmber
import com.aperture.wheatley.ui.theme.ApertureTextDim

@Composable
private fun ComingSoon(title: String, blurb: String) {
    Column(
        Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(title.uppercase(), style = MaterialTheme.typography.titleMedium, color = ApertureAmber)
        Text(blurb, style = MaterialTheme.typography.bodySmall, color = ApertureTextDim, textAlign = TextAlign.Center)
    }
}

@Composable
fun CameraScreen() = ComingSoon(
    "Camera · Phase 3",
    "Live snapshot feed + face-recognition overlay + \"look at this\" vision chat land in the camera phase.",
)

@Composable
fun FacesScreen() = ComingSoon(
    "Faces · Phase 4",
    "Known-face roster, rename/delete/enroll, per-person greetings, and the visitor log land in the faces phase.",
)
