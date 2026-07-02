package com.aperture.wheatley.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

// ---- Aperture Laboratories palette ---------------------------------------
val ApertureBlack = Color(0xFF050607)
val ApertureSurface = Color(0xFF0E1113)
val AperturePanel = Color(0xFF12171B)
val ApertureAmber = Color(0xFFF5A623)   // Aperture signage amber
val AperturePortal = Color(0xFF4FC3F7)  // portal blue
val ApertureText = Color(0xFFE6EDF0)
val ApertureTextDim = Color(0xFF7C8B93)
val ApertureOutline = Color(0xFF243138)
val ApertureDanger = Color(0xFFFF5A4D)
val ApertureGood = Color(0xFF57D9A3)

private val ApertureColors = darkColorScheme(
    primary = ApertureAmber,
    onPrimary = ApertureBlack,
    secondary = AperturePortal,
    onSecondary = ApertureBlack,
    background = ApertureBlack,
    onBackground = ApertureText,
    surface = ApertureSurface,
    onSurface = ApertureText,
    surfaceVariant = AperturePanel,
    onSurfaceVariant = ApertureTextDim,
    outline = ApertureOutline,
    error = ApertureDanger,
    onError = ApertureBlack,
)

// Industrial / terminal feel: monospace throughout.
private val mono = FontFamily.Monospace
private val ApertureType = Typography(
    titleLarge = TextStyle(fontFamily = mono, fontWeight = FontWeight.Bold, fontSize = 22.sp, letterSpacing = 2.sp),
    titleMedium = TextStyle(fontFamily = mono, fontWeight = FontWeight.Bold, fontSize = 16.sp, letterSpacing = 1.5.sp),
    labelLarge = TextStyle(fontFamily = mono, fontWeight = FontWeight.Bold, fontSize = 13.sp, letterSpacing = 1.sp),
    labelMedium = TextStyle(fontFamily = mono, fontWeight = FontWeight.Medium, fontSize = 11.sp, letterSpacing = 1.sp),
    bodyLarge = TextStyle(fontFamily = mono, fontSize = 14.sp),
    bodyMedium = TextStyle(fontFamily = mono, fontSize = 13.sp),
    bodySmall = TextStyle(fontFamily = mono, fontSize = 11.sp, color = ApertureTextDim),
)

@Composable
fun ApertureTheme(content: @Composable () -> Unit) {
    // Always dark — Aperture is a dark console regardless of system setting.
    MaterialTheme(
        colorScheme = ApertureColors,
        typography = ApertureType,
        content = content,
    )
}
