package com.jarvis.voice.wear.tile

import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.DimensionBuilders
import androidx.wear.protolayout.LayoutElementBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.TimelineBuilders
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import com.jarvis.voice.wear.R

/**
 * Tile JARVIS: mostra il Robocat (statico) — al tap apre MainActivity, che parte già in
 * ascolto. Le Tile non possono usare mic/animazioni live: fanno solo da lanciatore.
 */
class JarvisTileService : TileService() {

    private val version = "1"
    private val imgId = "robocat"

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
            .setWidth(DimensionBuilders.expand())
            .setHeight(DimensionBuilders.expand())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .setVerticalAlignment(LayoutElementBuilders.VERTICAL_ALIGN_CENTER)
            .addContent(
                LayoutElementBuilders.Image.Builder()
                    .setResourceId(imgId)
                    .setWidth(DimensionBuilders.dp(84f))
                    .setHeight(DimensionBuilders.dp(84f))
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
        val res = ResourceBuilders.Resources.Builder()
            .setVersion(version)
            .addIdToImageMapping(
                imgId,
                ResourceBuilders.ImageResource.Builder()
                    .setAndroidResourceByResId(
                        ResourceBuilders.AndroidImageResourceByResId.Builder()
                            .setResourceId(R.drawable.robocat_idle)
                            .build()
                    )
                    .build()
            )
            .build()
        return Futures.immediateFuture(res)
    }
}
