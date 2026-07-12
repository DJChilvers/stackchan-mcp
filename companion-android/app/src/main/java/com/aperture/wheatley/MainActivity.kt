package com.aperture.wheatley

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.Face
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.style.TextOverflow
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavDestination.Companion.hierarchy
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.aperture.wheatley.ui.screens.CameraScreen
import com.aperture.wheatley.ui.screens.ControlScreen
import com.aperture.wheatley.ui.screens.FacesScreen
import com.aperture.wheatley.ui.screens.PerformScreen
import com.aperture.wheatley.ui.screens.StatusScreen
import com.aperture.wheatley.ui.theme.ApertureAmber
import com.aperture.wheatley.ui.theme.ApertureBlack
import com.aperture.wheatley.ui.theme.ApertureTheme
import com.aperture.wheatley.ui.theme.ApertureTextDim

private enum class Dest(val route: String, val label: String, val icon: ImageVector) {
    CONTROL("control", "Control", Icons.Filled.Build),
    CAMERA("camera", "Camera", Icons.Filled.Search),
    PERFORM("perform", "Perform", Icons.Filled.PlayArrow),
    FACES("faces", "Faces", Icons.Filled.Face),
    STATUS("status", "Status", Icons.Filled.Info),
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            ApertureTheme { AppRoot() }
        }
    }
}

@Composable
private fun AppRoot(vm: MainViewModel = viewModel()) {
    val nav = rememberNavController()
    val snackbar = remember { SnackbarHostState() }

    // Surface transient action feedback as snackbars.
    androidx.compose.runtime.LaunchedEffect(Unit) {
        vm.messages.collect { snackbar.showSnackbar(it) }
    }

    Scaffold(
        containerColor = ApertureBlack,
        snackbarHost = { SnackbarHost(snackbar) },
        bottomBar = {
            val backStack by nav.currentBackStackEntryAsState()
            val current = backStack?.destination
            NavigationBar(containerColor = com.aperture.wheatley.ui.theme.ApertureSurface) {
                Dest.entries.forEach { dest ->
                    val selected = current?.hierarchy?.any { it.route == dest.route } == true
                    NavigationBarItem(
                        selected = selected,
                        onClick = {
                            nav.navigate(dest.route) {
                                popUpTo(nav.graph.findStartDestination().id) { saveState = true }
                                launchSingleTop = true
                                restoreState = true
                            }
                        },
                        icon = { Icon(dest.icon, contentDescription = dest.label) },
                        label = { Text(dest.label, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = ApertureBlack,
                            selectedTextColor = ApertureAmber,
                            indicatorColor = ApertureAmber,
                            unselectedIconColor = ApertureTextDim,
                            unselectedTextColor = ApertureTextDim,
                        ),
                    )
                }
            }
        },
    ) { pad ->
        Box(Modifier.fillMaxSize().padding(pad)) {
            NavHost(nav, startDestination = Dest.CONTROL.route) {
                composable(Dest.CONTROL.route) { ControlScreen(vm) }
                composable(Dest.CAMERA.route) { CameraScreen(vm) }
                composable(Dest.PERFORM.route) { PerformScreen(vm) }
                composable(Dest.FACES.route) { FacesScreen(vm) }
                composable(Dest.STATUS.route) { StatusScreen(vm) }
            }
        }
    }
}
