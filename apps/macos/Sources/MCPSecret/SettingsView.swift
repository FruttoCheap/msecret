import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        @Bindable var app = app
        Form {
            Section(L("Core")) {
                TextField(L("CLI"), text: $app.cliPath)
                TextField(L("Data root"), text: $app.dataRoot, prompt: Text(L("Default")))
                LabeledContent(L("In use"), value: app.status?.root ?? "—")
                Text(L("Changes last until you quit. Press Return to apply."))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section(L("Models")) {
                LabeledContent(L("Status"), value: app.status.map { $0.models ? L("Downloaded") : L("Missing") } ?? "—")
                if let job = app.job { JobView(job: job) }
                Button(L("Download Models")) { Task { await app.downloadModels() } }
                    .disabled(app.job != nil)
            }
            Section(L("Claude Desktop")) {
                LabeledContent(L("Status"), value: app.desktop.map { $0.configured ? L("Connected") : L("Not connected") } ?? "—")
                if let d = app.desktop, d.configured, !d.commandOk {
                    Label(L("The configured CLI no longer exists: install again."), systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                }
                LabeledContent(L("Last used by Claude"),
                               value: app.desktop?.lastLog?.formatted(date: .abbreviated, time: .shortened) ?? L("Never"))
                HStack {
                    Button(L("Install")) { Task { await app.desktopConfig("--install") } }
                    Button(L("Remove")) { Task { await app.desktopConfig("--uninstall") } }
                        .disabled(app.desktop?.configured != true)
                }
                if app.restartClaude {
                    Label(L("Restart Claude Desktop to apply the change."), systemImage: "arrow.clockwise.circle")
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 540)
        .onSubmit { Task { await app.refresh(); await app.desktopConfig() } }
        .task { await app.refresh(); await app.desktopConfig() }
    }
}
