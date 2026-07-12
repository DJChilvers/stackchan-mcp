package com.aperture.wheatley.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aperture.wheatley.Link
import com.aperture.wheatley.MainViewModel
import com.aperture.wheatley.data.GatewayConfig
import com.aperture.wheatley.data.Outcome
import com.aperture.wheatley.ui.components.AperturePanel
import com.aperture.wheatley.ui.components.ApertureButton
import com.aperture.wheatley.ui.components.StatRow
import com.aperture.wheatley.ui.theme.ApertureAmber
import com.aperture.wheatley.ui.theme.ApertureDanger
import com.aperture.wheatley.ui.theme.ApertureGood
import com.aperture.wheatley.ui.theme.ApertureTextDim
import kotlinx.coroutines.launch

@Composable
fun StatusScreen(vm: MainViewModel) {
    val ui by vm.ui.collectAsStateWithLifecycle()
    val cfg by vm.config.collectAsStateWithLifecycle()

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("APERTURE SCIENCE · WHEATLEY", style = androidx.compose.material3.MaterialTheme.typography.titleLarge, color = ApertureAmber)

        val (linkText, linkColor) = when (ui.link) {
            Link.ONLINE -> "● ONLINE" to ApertureGood
            Link.DEVICE_OFFLINE -> "● GATEWAY UP · DEVICE ASLEEP" to ApertureAmber
            Link.UNREACHABLE -> "● GATEWAY UNREACHABLE" to ApertureDanger
            Link.UNKNOWN -> "● …" to ApertureTextDim
        }
        Text(linkText, color = linkColor, style = androidx.compose.material3.MaterialTheme.typography.labelLarge)

        AperturePanel("Telemetry") {
            val s = ui.status
            val batt = s?.battery
            StatRow("Battery", batt?.level?.let { "$it%${if (batt.charging == true) " ⚡" else ""}" } ?: "—",
                valueColor = when {
                    batt?.level == null -> ApertureTextDim
                    batt.level < 20 && batt.charging != true -> ApertureDanger
                    else -> ApertureGood
                })
            StatRow("Volume", s?.volume?.toString() ?: "—")
            StatRow("Brightness", s?.brightness?.toString() ?: "—")
            StatRow("Device", s?.deviceId ?: "—")
            StatRow("Uptime", s?.uptimeS?.let { formatUptime(it) } ?: "—")
            s?.deviceStatusError?.let { StatRow("Status err", it, ApertureDanger) }
        }

        OrientationPanel(vm)

        SettingsPanel(cfg) { vm.saveConfig(it) }

        Text(
            "LAN control console for Wheatley.",
            style = MaterialTheme.typography.bodySmall,
            color = ApertureTextDim,
        )
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun OrientationPanel(vm: MainViewModel) {
    val scope = rememberCoroutineScope()
    var upsideDown by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        (vm.client().getOrientation() as? Outcome.Ok)?.let { upsideDown = it.data }
    }

    AperturePanel("Orientation") {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Mounted upside-down", color = ApertureAmber, style = MaterialTheme.typography.bodyMedium)
                Text(
                    "Flips the camera 180° and mirrors the head controls — for the rail / inverted look-down mount.",
                    color = ApertureTextDim, style = MaterialTheme.typography.bodySmall,
                )
            }
            Spacer(Modifier.width(12.dp))
            Switch(checked = upsideDown, onCheckedChange = { want ->
                upsideDown = want
                scope.launch {
                    when (val r = vm.client().setOrientation(want)) {
                        is Outcome.Ok -> vm.toast(if (want) "Upside-down ON" else "Upside-down OFF")
                        is Outcome.Err -> { vm.toast("Orientation ✗ ${r.message}"); upsideDown = !want }
                    }
                }
            })
        }
    }
}

@Composable
private fun SettingsPanel(cfg: GatewayConfig, onSave: (GatewayConfig) -> Unit) {
    var host by rememberSaveable(cfg.host) { mutableStateOf(cfg.host) }
    var port by rememberSaveable(cfg.port) { mutableStateOf(cfg.port.toString()) }
    var token by rememberSaveable(cfg.token) { mutableStateOf(cfg.token) }

    AperturePanel("Gateway connection") {
        OutlinedTextField(
            value = host, onValueChange = { host = it },
            label = { Text("Host / LAN IP") }, singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(
                value = port, onValueChange = { port = it.filter(Char::isDigit) },
                label = { Text("Port") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                modifier = Modifier.width(120.dp),
            )
        }
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = token, onValueChange = { token = it },
            label = { Text("Token (optional)") }, singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))
        ApertureButton("Save & connect", modifier = Modifier.fillMaxWidth()) {
            onSave(GatewayConfig(host.trim(), port.toIntOrNull() ?: 8770, token.trim()))
        }
    }
}

private fun formatUptime(s: Long): String {
    val h = s / 3600; val m = (s % 3600) / 60
    return if (h > 0) "${h}h ${m}m" else "${m}m ${s % 60}s"
}
