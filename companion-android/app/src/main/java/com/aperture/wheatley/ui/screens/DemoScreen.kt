package com.aperture.wheatley.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.aperture.wheatley.MainViewModel
import com.aperture.wheatley.ui.components.AperturePanel
import com.aperture.wheatley.ui.components.ApertureButton
import com.aperture.wheatley.ui.components.ApertureOutlineButton
import com.aperture.wheatley.ui.theme.ApertureTextDim

// Reaction behaviours exposed by the gateway's sensor reactor.
private val REACTIONS = listOf("panic", "hacker", "tantrum", "overtrack", "lights_out")

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun DemoScreen(vm: MainViewModel) {
    Column(
        Modifier.fillMaxSize().padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        AperturePanel("Showcase") {
            Text("Runs a short choreographed routine: moves, expressions, a couple of lines, LEDs.",
                style = MaterialTheme.typography.bodySmall, color = ApertureTextDim)
            Spacer(Modifier.height(12.dp))
            ApertureButton("▶  Run demo", modifier = Modifier.fillMaxWidth().height(56.dp)) {
                vm.act("Demo") { it.demo() }
            }
        }

        AperturePanel("Individual reactions") {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                REACTIONS.forEach { r ->
                    ApertureOutlineButton(r.replace('_', ' ')) { vm.act(r) { c -> c.react(r) } }
                }
            }
        }
    }
}
