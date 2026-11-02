// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "MCPSecret",
    defaultLocalization: "en",
    platforms: [.macOS(.v15)],
    targets: [
        .executableTarget(
            name: "MCPSecret",
            resources: [.process("Localizable.xcstrings"), .process("Resources")],
            // An unbundled executable declares its languages in an embedded Info.plist: without
            // it Foundation picks English whatever the system language is.
            linkerSettings: [.unsafeFlags(["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
                                           "-Xlinker", "__info_plist",
                                           "-Xlinker", "\(Context.packageDirectory)/Info.plist"])]),
        .testTarget(name: "MCPSecretTests", dependencies: ["MCPSecret"]),
    ]
)
