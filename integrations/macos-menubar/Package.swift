// swift-tools-version:5.9
// The agent-comms menu bar app for the human. No third-party dependencies.
import PackageDescription

let package = Package(
    name: "AgentCommsMenuBar",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "AgentCommsMenuBar", targets: ["AgentCommsMenuBar"]),
    ],
    targets: [
        // Everything testable without a UI: summary decoding, the token-file check, URLs, uv lookup, labels.
        .target(name: "AgentCommsKit"),
        // The SwiftUI MenuBarExtra app.
        .executableTarget(name: "AgentCommsMenuBar", dependencies: ["AgentCommsKit"]),
        .testTarget(
            name: "AgentCommsMenuBarTests",
            dependencies: ["AgentCommsKit"],
            resources: [.copy("Fixtures")]
        ),
    ]
)
