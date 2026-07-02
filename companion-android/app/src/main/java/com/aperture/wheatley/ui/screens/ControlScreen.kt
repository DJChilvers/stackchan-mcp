package com.aperture.wheatley.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.aperture.wheatley.MainViewModel
import com.aperture.wheatley.ui.components.AperturePanel
import com.aperture.wheatley.ui.components.ApertureButton
import com.aperture.wheatley.ui.components.ApertureOutlineButton
import com.aperture.wheatley.ui.theme.ApertureTextDim

private val FACES = listOf("idle", "happy", "thinking", "sad", "surprised", "embarrassed")
private val LED_SWATCHES = listOf(
    Triple("Blue", 0x4F to 0xC3, 0xF7),
    Triple("Amber", 0xF5 to 0xA6, 0x23),
    Triple("Red", 0xFF to 0x30, 0x20),
    Triple("Green", 0x30 to 0xD0, 0x60),
    Triple("Purple", 0x9B to 0x59, 0xF6),
)

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun ControlScreen(vm: MainViewModel) {
    var yaw by remember { mutableFloatStateOf(0f) }
    var pitch by remember { mutableFloatStateOf(45f) }
    var torque by remember { mutableStateOf(true) }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        AperturePanel("Head servos") {
            Text("Yaw  ${yaw.toInt()}°", style = MaterialTheme.typography.bodySmall, color = ApertureTextDim)
            Slider(
                value = yaw, onValueChange = { yaw = it }, valueRange = -80f..80f,
                onValueChangeFinished = { vm.act("Head") { it.head(yaw.toInt(), pitch.toInt()) } },
            )
            Text("Pitch  ${pitch.toInt()}°  (low = look at you, high = up)",
                style = MaterialTheme.typography.bodySmall, color = ApertureTextDim)
            Slider(
                value = pitch, onValueChange = { pitch = it }, valueRange = 10f..80f,
                onValueChangeFinished = { vm.act("Head") { it.head(yaw.toInt(), pitch.toInt()) } },
            )
            Row {
                ApertureOutlineButton("Centre", modifier = Modifier.weight(1f)) {
                    yaw = 0f; pitch = 45f; vm.act("Centre") { it.head(0, 45) }
                }
            }
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Servo torque", modifier = Modifier.weight(1f))
                Switch(checked = torque, onCheckedChange = {
                    torque = it; vm.act("Torque") { c -> c.torque(torque, torque) }
                })
            }
        }

        AperturePanel("Management rail motor") {
            Text("Firmware pending — motor tool not on this flash yet.",
                style = MaterialTheme.typography.bodySmall, color = ApertureTextDim)
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ApertureButton("◀ Left", modifier = Modifier.weight(1f), enabled = false) {}
                ApertureButton("Right ▶", modifier = Modifier.weight(1f), enabled = false) {}
            }
        }

        AperturePanel("Expression") {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                FACES.forEach { f ->
                    ApertureOutlineButton(f) { vm.act("Face $f") { it.avatar(f) } }
                }
            }
        }

        AperturePanel("Base ring LEDs") {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                LED_SWATCHES.forEach { (name, rg, b) ->
                    val (r, g) = rg
                    ApertureOutlineButton(name) { vm.act("LED $name") { it.leds(r, g, b) } }
                }
                ApertureOutlineButton("Off") { vm.act("LED off") { it.ledsClear() } }
            }
        }
        Spacer(Modifier.height(24.dp))
    }
}
