package com.streamvix.plugins

import com.streamvix.routes.*
import io.ktor.server.application.*
import io.ktor.server.routing.*
import io.ktor.server.http.content.*
import java.io.File

fun Application.configureRouting() {
    routing {
        // Static files
        staticFiles("/static", File("static"))
        
        // Main routes
        rootRoutes()
        proxyRoutes()
        infoRoutes()
        playlistRoutes()
        licenseRoutes()
        keyRoutes()
    }
}
