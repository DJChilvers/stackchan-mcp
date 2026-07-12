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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aperture.wheatley.MainViewModel
import com.aperture.wheatley.ui.components.AperturePanel
import com.aperture.wheatley.ui.components.ApertureButton
import com.aperture.wheatley.ui.components.ApertureOutlineButton
import com.aperture.wheatley.ui.theme.ApertureTextDim

// Reaction behaviours exposed by the gateway's sensor reactor.
private val REACTIONS = listOf("panic", "hacker", "tantrum", "overtrack", "lights_out")

/**
 * Combined "Perform" screen: everything that makes Wheatley speak or put on a
 * show — category sayings, free-text speech, the demo routine, and one-off
 * reaction triggers. (Merges the former Sayings + Demo tabs.)
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun PerformScreen(vm: MainViewModel) {
    val ui by vm.ui.collectAsStateWithLifecycle()
    var freeText by remember { mutableStateOf("") }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        AperturePanel("Demo") {
            Text(
                "Runs a short choreographed routine: moves, expressions, a couple of lines, LEDs.",
                style = MaterialTheme.typography.bodySmall, color = ApertureTextDim,
            )
            Spacer(Modifier.height(12.dp))
            ApertureButton("▶  Run demo", modifier = Modifier.fillMaxWidth().height(56.dp)) {
                vm.act("Demo") { it.demo() }
            }
        }

        AperturePanel("Sayings") {
            Text(
                "Pick a category — Wheatley says a fresh line each tap.",
                style = MaterialTheme.typography.bodySmall, color = ApertureTextDim,
            )
            Spacer(Modifier.height(10.dp))
            if (ui.categories.isEmpty()) {
                Text(
                    "(no categories — check the connection)",
                    style = MaterialTheme.typography.bodySmall, color = ApertureTextDim,
                )
            } else {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    ui.categories.forEach { cat ->
                        ApertureOutlineButton(cat.label) { vm.act(cat.label) { it.sayPreset(cat.key) } }
                    }
                }
            }
        }

        AperturePanel("Say anything") {
            OutlinedTextField(
                value = freeText, onValueChange = { freeText = it },
                label = { Text("Type a line for Wheatley to speak") },
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(10.dp))
            ApertureButton("Speak", modifier = Modifier.fillMaxWidth(), enabled = freeText.isNotBlank()) {
                val t = freeText.trim()
                vm.act("Say") { it.say(t) }
            }
        }

        AperturePanel("Reactions") {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                REACTIONS.forEach { r ->
                    ApertureOutlineButton(r.replace('_', ' ')) { vm.act(r) { c -> c.react(r) } }
                }
            }
        }
        Spacer(Modifier.height(24.dp))
    }
}
