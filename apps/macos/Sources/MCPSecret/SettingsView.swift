import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var app
    @State private var ruleName = ""
    @State private var ruleTerms = ""
    @State private var folderLink = ""
    @State private var choosingClient = false

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
            Section(L("Google Drive")) {
                Text(L("The folders you add are copied to this Mac, in the data root's private folder (Docs, Sheets and Slides as Office files), then indexed like your other documents. Google grants read-only access."))
                    .font(.caption).foregroundStyle(.secondary)
                LabeledContent(L("Status"), value: app.drive.map { $0.connected ? L("Connected") : L("Not connected") } ?? "—")
                HStack {
                    // the first time: the OAuth client file (Desktop app) of a Google Cloud project
                    Button(L("Connect…")) {
                        if app.drive?.client == true { Task { await app.connectDrive(client: nil) } } else { choosingClient = true }
                    }
                    Button(L("Disconnect")) { Task { await app.driveCommand("logout") } }
                        .disabled(app.drive?.connected != true)
                }
                .disabled(app.job != nil)
                ForEach(app.drive?.folders ?? []) { folder in
                    HStack {
                        Label(folder.name, systemImage: "folder")
                        Spacer()
                        Button { Task { await app.changeDrive("remove", folder.id) } } label: {
                            Label(L("Remove"), systemImage: "trash")
                        }
                        .labelStyle(.iconOnly)
                    }
                }
                TextField(L("Folder link"), text: $folderLink,
                          prompt: Text(verbatim: "https://drive.google.com/drive/folders/…"))
                HStack {
                    Button(L("Add Folder")) {
                        let link = folderLink
                        folderLink = ""
                        Task { await app.changeDrive("add", link) }
                    }
                    .disabled(folderLink.trimmingCharacters(in: .whitespaces).isEmpty)
                    Button(L("Sync Now")) { Task { await app.syncDrive() } }
                        .disabled(app.drive?.folders.isEmpty != false)
                }
                .disabled(app.job != nil || app.drive?.connected != true)
            }
            .fileImporter(isPresented: $choosingClient, allowedContentTypes: [.json]) { result in
                if case .success(let url) = result { Task { await app.connectDrive(client: url.path) } }
            }
            Section(L("Automatic Categories")) {
                Text(L("Each document gets the name of its folder, and the category of every rule with a term in what the agent sees: a word, a prefix* or a phrase. Right-click a document to add or remove categories by hand."))
                    .font(.caption).foregroundStyle(.secondary)
                ForEach(app.rules, id: \.name) { rule in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(verbatim: rule.name).bold()
                            Text(verbatim: rule.terms.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button(L("Edit")) { (ruleName, ruleTerms) = (rule.name, rule.terms.joined(separator: ", ")) }
                        Button { Task { await app.setRule(rule.name, terms: []) } } label: {
                            Label(L("Remove"), systemImage: "trash")
                        }
                        .labelStyle(.iconOnly)
                    }
                }
                TextField(L("Category"), text: $ruleName)
                TextField(L("Terms, comma-separated"), text: $ruleTerms)
                Button(L("Save Rule")) {
                    let (name, terms) = (ruleName, ruleTerms.split(separator: ",").map(String.init))
                    Task { await app.setRule(name, terms: terms) }
                    (ruleName, ruleTerms) = ("", "")
                }
                .disabled(ruleName.trimmingCharacters(in: .whitespaces).isEmpty || ruleName.contains(":")
                          || ruleTerms.trimmingCharacters(in: .whitespaces).isEmpty)
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
        .task { await app.refresh(); await app.desktopConfig(); await app.loadRules(); await app.driveCommand() }
    }
}
