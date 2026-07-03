package com.jarvis.voice.wear.tile

import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.LayoutElementBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.TimelineBuilders
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture

/**
 * Tile JARVIS: la "scheda" che scorri sul quadrante. Un tap apre MainActivity,
 * che avvia subito l'ascolto (tap sulla testa robot).
 *
 * NB: le Tile non possono accedere al microfono; per questo il tap lancia l'Activity.
 */
class JarvisTileService : TileService() {

    private val version = "1"

    override fun onTileRequest(
        requestParams: RequestBuilders.TileRequest
    ): ListenableFuture<TileBuilders.Tile> {
        val click = ModifiersBuilders.Clickable.Builder()
            .setId("open")
            .setOnClick(
                ActionBuilders.LaunchAction.Builder()
                    .setAndroidActivity(
                        ActionBuilders.AndroidActivity.Builder()
                            .setPackageName(packageName)
                            .setClassName("com.jarvis.voice.wear.MainActivity")
                            .build()
                    )
                    .build()
            )
            .build()

        val root = LayoutElementBuilders.Box.Builder()
            .addContent(
                LayoutElementBuilders.Text.Builder()
                    .setText("JARVIS")
                    .setModifiers(
                        ModifiersBuilders.Modifiers.Builder().setClickable(click).build()
                    )
                    .build()
            )
            .build()

        val tile = TileBuilders.Tile.Builder()
            .setResourcesVersion(version)
            .setTileTimeline(TimelineBuilders.Timeline.fromLayoutElement(root))
            .build()

        return Futures.immediateFuture(tile)
    }

    override fun onTileResourcesRequest(
        requestParams: RequestBuilders.ResourcesRequest
    ): ListenableFuture<ResourceBuilders.Resources> {
        return Futures.immediateFuture(
            ResourceBuilders.Resources.Builder().setVersion(version).build()
        )
    }
}
