package com.aperture.wheatley.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.aperture.wheatley.ui.theme.ApertureAmber
import com.aperture.wheatley.ui.theme.ApertureOutline
import com.aperture.wheatley.ui.theme.AperturePanel
import com.aperture.wheatley.ui.theme.AperturePortal
import com.aperture.wheatley.ui.theme.ApertureTextDim

/** A bordered console panel with an amber section label, à la Aperture signage. */
@Composable
fun AperturePanel(
    title: String,
    modifier: Modifier = Modifier,
    content: @Composable Column.() -> Unit,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(4.dp),
        colors = CardDefaults.cardColors(containerColor = AperturePanel),
        border = BorderStroke(1.dp, ApertureOutline),
    ) {
        Column(Modifier.padding(14.dp)) {
            SectionHeader(title)
            content()
        }
    }
}

@Composable
fun SectionHeader(text: String, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier.fillMaxWidth().padding(bottom = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier
                .padding(end = 8.dp)
                .clip(RoundedCornerShape(1.dp))
                .background(ApertureAmber)
                .padding(horizontal = 3.dp, vertical = 7.dp),
        ) {}
        Text(
            text.uppercase(),
            style = MaterialTheme.typography.labelLarge,
            color = ApertureAmber,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/** Primary amber action button. */
@Composable
fun ApertureButton(
    label: String,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    onClick: () -> Unit,
) {
    Button(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier,
        shape = RoundedCornerShape(3.dp),
        colors = ButtonDefaults.buttonColors(
            containerColor = ApertureAmber,
            contentColor = Color.Black,
            disabledContainerColor = ApertureOutline,
            disabledContentColor = ApertureTextDim,
        ),
    ) { Text(label.uppercase(), style = MaterialTheme.typography.labelLarge) }
}

/** Secondary outlined button (portal blue). */
@Composable
fun ApertureOutlineButton(
    label: String,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    onClick: () -> Unit,
) {
    OutlinedButton(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier,
        shape = RoundedCornerShape(3.dp),
        border = BorderStroke(1.dp, if (enabled) AperturePortal else ApertureOutline),
    ) {
        Text(
            label.uppercase(),
            style = MaterialTheme.typography.labelLarge,
            color = if (enabled) AperturePortal else ApertureTextDim,
        )
    }
}

/** A key/value telemetry row. */
@Composable
fun StatRow(key: String, value: String, valueColor: Color = MaterialTheme.colorScheme.onSurface) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(key.uppercase(), style = MaterialTheme.typography.bodySmall, color = ApertureTextDim)
        Text(value, style = MaterialTheme.typography.bodyMedium, color = valueColor)
    }
}
