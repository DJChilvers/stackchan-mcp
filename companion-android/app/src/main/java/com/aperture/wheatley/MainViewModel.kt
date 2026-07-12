package com.aperture.wheatley

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aperture.wheatley.data.AppSettings
import com.aperture.wheatley.data.GatewayClient
import com.aperture.wheatley.data.GatewayConfig
import com.aperture.wheatley.data.Outcome
import com.aperture.wheatley.data.SayCategory
import com.aperture.wheatley.data.StatusResponse
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

enum class Link { UNKNOWN, ONLINE, DEVICE_OFFLINE, UNREACHABLE }

data class UiState(
    val link: Link = Link.UNKNOWN,
    val status: StatusResponse? = null,
    val categories: List<SayCategory> = emptyList(),
)

class MainViewModel(app: Application) : AndroidViewModel(app) {
    private val settings = AppSettings(app)

    val config: StateFlow<GatewayConfig> =
        settings.config.stateIn(viewModelScope, SharingStarted.Eagerly, GatewayConfig())

    private val _ui = MutableStateFlow(UiState())
    val ui: StateFlow<UiState> = _ui

    private val _messages = Channel<String>(Channel.BUFFERED)
    val messages = _messages.receiveAsFlow()

    // "Look at this" vision chat state (Camera screen).
    private val _visionBusy = MutableStateFlow(false)
    val visionBusy: StateFlow<Boolean> = _visionBusy
    private val _visionAnswer = MutableStateFlow<String?>(null)
    val visionAnswer: StateFlow<String?> = _visionAnswer

    fun client() = GatewayClient(config.value)

    init {
        // Poll telemetry while the app is alive.
        viewModelScope.launch {
            while (true) {
                refreshStatus()
                delay(3000)
            }
        }
        viewModelScope.launch { loadCategories() }
    }

    suspend fun refreshStatus() {
        when (val r = client().status()) {
            is Outcome.Ok -> _ui.value = _ui.value.copy(
                status = r.data,
                link = if (r.data.connected) Link.ONLINE else Link.DEVICE_OFFLINE,
            )
            is Outcome.Err -> _ui.value = _ui.value.copy(link = Link.UNREACHABLE)
        }
    }

    private suspend fun loadCategories() {
        when (val r = client().sayCategories()) {
            is Outcome.Ok -> _ui.value = _ui.value.copy(categories = r.data)
            is Outcome.Err -> {} // non-fatal; buttons just stay empty
        }
    }

    fun saveConfig(cfg: GatewayConfig) = viewModelScope.launch {
        settings.save(cfg)
        // config flow updates; refresh immediately against the new endpoint.
        _ui.value = _ui.value.copy(link = Link.UNKNOWN)
        refreshStatus()
        loadCategories()
    }

    /** "Look at this": capture + Claude vision + speak; shows the answer. */
    fun askVision(question: String) = viewModelScope.launch {
        _visionBusy.value = true
        when (val r = client().visionAsk(question)) {
            is Outcome.Ok -> {
                _visionAnswer.value = r.data.answer
                _messages.trySend("Wheatley looked ✓")
            }
            is Outcome.Err -> _messages.trySend("Look ✗ ${r.message}")
        }
        _visionBusy.value = false
    }

    fun clearVisionAnswer() { _visionAnswer.value = null }

    /** Surface a one-off message through the shared snackbar. */
    fun toast(text: String) { _messages.trySend(text) }

    /** Run an API action and surface a short result message. */
    fun act(label: String, block: suspend (GatewayClient) -> Outcome<*>) = viewModelScope.launch {
        when (val r = block(client())) {
            is Outcome.Ok -> _messages.trySend("$label ✓")
            is Outcome.Err -> _messages.trySend("$label ✗ ${r.message}")
        }
        refreshStatus()
    }
}
