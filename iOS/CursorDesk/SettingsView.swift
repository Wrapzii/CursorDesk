import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: DeskModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Workstation") {
                    TextField("Host IP or Tailscale name", text: $model.hostAddress)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    TextField("Label", text: $model.projectLabel)
                }
                Section("How to use") {
                    Text("1. Run Host\\Install-And-Start.bat on the PC once.")
                    Text("2. Pair Moonlight (or this app) with Sunshine using the PIN.")
                    Text("3. Tap Enter Cursor here — stream the desktop.")
                    Text("4. In Cursor, use local Editor Agent for Unreal MCP.")
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Save") {
                        model.save()
                        dismiss()
                    }
                }
            }
        }
    }
}
